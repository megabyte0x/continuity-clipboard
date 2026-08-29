#!/usr/bin/env python3
"""Continuity Clipboard bridge daemon.

Bridges the Wayland clipboard (wl-copy / wl-paste) to iPhone Shortcuts over
token-authenticated LAN HTTP. Runs with plain user permissions, supervised by
the plugin's Service.qml; Python stdlib only.

Routes:
  GET  /ping    liveness, no auth
  GET  /clip    current desktop clipboard (text or image)
  POST /clip    set the desktop clipboard (text/*, image/*, form field, or sniffed)
  GET  /setup   phone onboarding page (QR target)
  GET  /        redirect to /setup
"""

import argparse
import errno
import fcntl
import hmac
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

APP = "continuity-clipboard"
SCRIPT_NAME = os.path.basename(__file__)
VERSION = "1.5.0"
DEFAULT_PORT = 8737
MAX_BODY_BYTES = 10 * 1024 * 1024
PREVIEW_CHARS = 80
SUBPROCESS_TIMEOUT = 5
IMAGE_MIME_PREFERENCE = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff")


def log(message):
    print(f"{APP}: {message}", file=sys.stderr, flush=True)


def default_state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "omarchy", APP)


def process_command(pid):
    """argv of `pid` as a list, or [] when it is gone / not ours to read."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return [part.decode("utf-8", "replace") for part in handle.read().split(b"\0") if part]
    except OSError:
        return []


def is_sibling_daemon(pid):
    """True when `pid` is another running copy of this daemon.

    Deliberately strict: the match is `python* clipbridged.py ...` (or a direct
    shebang exec), never "any process whose command line mentions the script".
    A loose substring test also matches the shell, editor, tail, or grep a user
    happens to be running against this file -- and this predicate gates SIGKILL.
    """
    if pid <= 0 or pid == os.getpid():
        return False
    argv = process_command(pid)
    if not argv:
        return False
    program = os.path.basename(argv[0])
    if program == SCRIPT_NAME:
        return True
    if not program.startswith("python"):
        return False
    for arg in argv[1:]:
        if arg.startswith("-"):
            continue
        return os.path.basename(arg) == SCRIPT_NAME
    return False


def claim_singleton(state_dir, timeout=6.0):
    """Take the daemon's exclusive lock, reclaiming it from an orphan if needed.

    One machine runs one bridge. When the shell that supervised the previous
    daemon dies without reaping it (SIGKILL, crash, `omarchy-restart-shell`),
    that daemon is reparented to init and keeps the port -- so every daemon the
    new shell starts died on bind and the supervisor retried forever. The lock
    is an fcntl advisory lock held on an open descriptor, so the kernel drops it
    the instant the holder exits by any means: no stale-pidfile heuristics.

    Returns the held descriptor (keep it alive for the process lifetime), or
    None when a live sibling refuses to yield.
    """
    path = os.path.join(state_dir, "daemon.lock")
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

    def try_lock():
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            return False

    if not try_lock():
        try:
            holder = int(os.pread(handle, 32, 0).decode("utf-8", "replace").strip() or 0)
        except ValueError:
            holder = 0
        if not is_sibling_daemon(holder):
            # Lock held by something we cannot identify as ours: never signal it.
            log(f"another process (pid {holder or 'unknown'}) holds {path}; not starting")
            os.close(handle)
            return None
        if not is_orphaned(holder):
            # Same rule as the sweep below: a daemon with a live supervisor is
            # not ours to reclaim, or the two shells trade kills forever.
            log(f"another shell already supervises the bridge (pid {holder}); not starting")
            os.close(handle)
            return None
        log(f"reclaiming the bridge from orphaned daemon pid {holder}")
        for signal_number in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(holder, signal_number)
            except ProcessLookupError:
                pass
            except PermissionError:
                log(f"cannot signal pid {holder}; not starting")
                os.close(handle)
                return None
            deadline = time.monotonic() + (timeout if signal_number == signal.SIGTERM else 2.0)
            while time.monotonic() < deadline:
                if try_lock():
                    os.ftruncate(handle, 0)
                    os.pwrite(handle, f"{os.getpid()}\n".encode("utf-8"), 0)
                    return handle
                time.sleep(0.1)
        log(f"orphaned daemon pid {holder} would not exit; not starting")
        os.close(handle)
        return None

    os.ftruncate(handle, 0)
    os.pwrite(handle, f"{os.getpid()}\n".encode("utf-8"), 0)
    return handle


def parent_pid(pid):
    """ppid from /proc/<pid>/stat, reading past a comm field that may contain ')'."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
            fields = handle.read().rpartition(")")[2].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return 0


def is_orphaned(pid):
    """True when `pid` lost its supervisor and was reparented to init/systemd.

    This daemon is only ever started by the shell plugin, so a live non-reaper
    parent means another running shell still owns that daemon. Reclaiming it
    would make the two supervisors kill each other's daemon forever; leaving it
    alone keeps 'newest shell wins' from becoming a restart loop.
    """
    ppid = parent_pid(pid)
    if ppid <= 1:
        return True
    parent_argv = process_command(ppid)
    return bool(parent_argv) and os.path.basename(parent_argv[0]) == "systemd"


def terminate_sibling_daemons(timeout=6.0):
    """Stop orphaned copies of this daemon left behind by a dead shell.

    The lock above is authoritative for daemons that know about it, but an
    orphan started by an older build holds no lock -- it only holds the port.
    Processes we cannot signal (another user's) raise PermissionError and are
    skipped rather than killed.

    Returns False when a sibling is still supervised by a live shell, which the
    caller treats as fatal instead of starting a kill war over the port.
    """
    victims = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if not is_sibling_daemon(pid):
            continue
        if not is_orphaned(pid):
            log(f"another shell already supervises the bridge (pid {pid}); not starting")
            return False
        victims.append(pid)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"stopped a previous bridge daemon (pid {pid})")
        except (ProcessLookupError, PermissionError):
            continue
    deadline = time.monotonic() + timeout
    while victims and time.monotonic() < deadline:
        victims = [pid for pid in victims if is_sibling_daemon(pid)]
        if victims:
            time.sleep(0.1)
    for pid in victims:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            continue
    return True


def bind_server(bind_host, port, attempts=10, delay=0.3):
    """Bind with short retries: a just-killed orphan's socket takes a beat to close."""
    last = None
    for attempt in range(attempts):
        try:
            return ThreadingHTTPServer((bind_host, port), Handler), None
        except OSError as error:
            last = error
            if error.errno != errno.EADDRINUSE:
                break
            time.sleep(delay)
    return None, last


def paired_marker_path(state_dir):
    return os.path.join(state_dir, "paired.json")


def load_paired_at(state_dir):
    """ISO timestamp of the first completed transfer, or None when unpaired."""
    try:
        with open(paired_marker_path(state_dir), encoding="utf-8") as handle:
            value = json.load(handle).get("paired_at")
        return str(value) if value else None
    except (OSError, ValueError, AttributeError):
        return None


def save_paired_at(state_dir):
    """Record the pairing once, so it survives daemon restarts. Returns the stamp."""
    stamp = datetime.now().isoformat(timespec="seconds")
    path = paired_marker_path(state_dir)
    temp = path + ".tmp"
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"paired_at": stamp}, handle)
        os.replace(temp, path)
    except OSError as error:
        log(f"could not persist pairing state: {error}")
    return stamp


def adopt_prior_pairing(state_dir):
    """Backfill the marker for phones paired before it existed.

    Upgrading users have a working phone shortcut but no paired.json, and the
    counters start at zero, so without this the panel would report "Not paired
    yet" until the next clip. A status.json showing past traffic is proof enough.
    """
    try:
        with open(os.path.join(state_dir, "status.json"), encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(status, dict):
        return None
    traffic = (status.get("received") or 0) + (status.get("served") or 0)
    if traffic <= 0 and not status.get("last_event"):
        return None
    return save_paired_at(state_dir)


def load_or_create_token(path):
    """Return the shared secret, minting one with owner-only permissions on first run."""
    try:
        with open(path, encoding="utf-8") as handle:
            token = handle.read().strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    return token


def lan_host():
    """Best-effort LAN address: the source address of a routed UDP socket."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("1.1.1.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def mdns_host():
    """Stable `<hostname>.local` name if it actually resolves, else None.

    A Bonjour/mDNS name survives DHCP address changes (new Wi-Fi network, lease
    renewal), so pairing does not break when the LAN IP moves. We only return it
    when the local resolver can turn it into an address, which on Linux means
    avahi/nss-mdns is present and the name is being advertised; iOS resolves the
    same name natively over Bonjour on the shared LAN.
    """
    try:
        name = socket.gethostname().split(".")[0]
    except OSError:
        return None
    if not name or name.lower() == "localhost":
        return None
    fqdn = name + ".local"
    try:
        socket.getaddrinfo(fqdn, None)
    except OSError:
        return None
    return fqdn


def pairing_host(override=None):
    """Host to embed in the pairing URL / QR.

    Priority: explicit override (arg/env, e.g. a Tailscale IP or custom name),
    then a resolvable mDNS `.local` name, then the raw routed LAN IP.
    """
    if override:
        return override
    return mdns_host() or lan_host()


def preview_for(mime, data):
    """Human line for notifications and status.json; never the full payload."""
    if mime.startswith("image/"):
        return f"image ({mime}, {max(1, len(data) // 1024)} KB)"
    text = data.decode("utf-8", "replace").strip().replace("\n", " ")
    if len(text) > PREVIEW_CHARS:
        text = text[: PREVIEW_CHARS - 1] + "…"
    return f'"{text}"'


def load_shortcut_links(state_dir):
    """Optional one-tap install links (shortcut-links.json in the state dir).

    Apple only imports Apple-signed shortcut files, and the sole sanctioned
    signed-distribution channel is an iCloud share link published once from
    an Apple device. When the owner publishes the two shortcuts and drops
    their links here, the setup page upgrades to one-tap Add buttons.
    """
    path = os.path.join(state_dir, "shortcut-links.json")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return {}
    links = {}
    for key in ("send", "get"):
        value = raw.get(key, "") if isinstance(raw, dict) else ""
        if isinstance(value, str) and value.startswith("https://www.icloud.com/shortcuts/"):
            links[key] = value
    return links


def sniff_mime(data):
    """Classify an untyped body: known image magics, else UTF-8 text, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand.startswith(b"hei") or brand in (b"mif1", b"msf1", b"hevc", b"hevx"):
            return "image/heic"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return None


# wl-copy offers exactly one mime type, and most Wayland apps only pull
# image/png from the clipboard — a jpeg-only (or heic-only) offer pastes as
# nothing. So every incoming image is normalized to PNG.
PASTE_FRIENDLY_IMAGES = ("image/png",)

# A body that is exactly a variable's *name* almost always means the user
# typed the word into the shortcut instead of inserting the blue variable
# token. The clip is still applied (the word is a legal clip), but the
# desktop notification teaches the fix.
LITERAL_VARIABLE_NAMES = ("Clipboard", "Shortcut Input")


def normalize_image(mime, data):
    """Convert every non-PNG image to PNG.

    ImageMagick sniffs the input format from the bytes; on any failure the
    original passes through unchanged (an odd-mime clip beats a lost one).
    """
    if mime in PASTE_FRIENDLY_IMAGES:
        return mime, data
    try:
        result = subprocess.run(["magick", "-", "png:-"], input=data,
                                capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        log(f"image conversion unavailable: {error}")
        return mime, data
    if result.returncode != 0 or not result.stdout:
        detail = result.stderr.decode("utf-8", "replace").strip()[:200]
        log(f"image conversion failed for {mime}: {detail}")
        return mime, data
    return "image/png", result.stdout


class Bridge:
    """Daemon state plus the wl-clipboard boundary."""

    def __init__(self, state_dir, port, host_override=None):
        self.state_dir = state_dir
        self.port = port
        self.token = load_or_create_token(os.path.join(state_dir, "token"))
        self.host_override = host_override
        # Resolve the stable pairing name once: the override and the mDNS
        # `<hostname>.local` are constant, and mDNS resolution is a network
        # lookup we must not repeat on the per-clip hot path below.
        self._stable_host = host_override or mdns_host()
        self.host = lan_host()
        self.pair_host = self._stable_host or self.host
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.received = 0
        self.served = 0
        self.last_event = None
        # Pairing is a property of the phone, not of this process: the counters
        # above reset on every restart, so the fact that a phone has completed a
        # transfer lives in the state dir instead.
        self.paired_at = load_paired_at(state_dir) or adopt_prior_pairing(state_dir)
        self._lock = threading.Lock()

    def write_status(self):
        status = {
            "ok": True,
            "app": APP,
            "version": VERSION,
            "pid": os.getpid(),
            "host": self.host,
            "pair_host": self.pair_host,
            "port": self.port,
            "started_at": self.started_at,
            "received": self.received,
            "served": self.served,
            "paired": self.paired_at is not None,
            "paired_at": self.paired_at,
            "last_event": self.last_event,
        }
        path = os.path.join(self.state_dir, "status.json")
        temp = path + ".tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(status, handle)
        os.replace(temp, path)

    def record(self, direction, mime, data):
        with self._lock:
            if direction == "phone-to-desktop":
                self.received += 1
            else:
                self.served += 1
            self.last_event = {
                "direction": direction,
                "kind": "image" if mime.startswith("image/") else "text",
                "preview": preview_for(mime, data),
                "at": datetime.now().strftime("%H:%M"),
            }
            if self.paired_at is None:
                self.paired_at = save_paired_at(self.state_dir)
            self.host = lan_host()
            # Stable host is cached; only the IP fallback can move between clips.
            self.pair_host = self._stable_host or self.host
            self.write_status()

    # -- Wayland clipboard ---------------------------------------------

    def clip_types(self):
        result = subprocess.run(["wl-paste", "--list-types"], capture_output=True, timeout=SUBPROCESS_TIMEOUT)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]

    def read_clip(self):
        """Snapshot the current selection as (mime, bytes); (None, b"") when empty."""
        types = self.clip_types()
        for mime in IMAGE_MIME_PREFERENCE:
            if mime in types:
                result = subprocess.run(["wl-paste", "--type", mime], capture_output=True, timeout=SUBPROCESS_TIMEOUT)
                if result.returncode == 0 and result.stdout:
                    return mime, result.stdout
        if any(t.startswith("text/") or t in ("UTF8_STRING", "STRING", "TEXT") for t in types):
            result = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, timeout=SUBPROCESS_TIMEOUT)
            if result.returncode == 0 and result.stdout:
                return "text/plain; charset=utf-8", result.stdout
        return None, b""

    def write_clip(self, mime, data):
        command = ["wl-copy"]
        if mime.startswith("image/"):
            command += ["--type", mime]
        subprocess.run(command, input=data, timeout=SUBPROCESS_TIMEOUT, check=True)

    def notify(self, preview):
        try:
            subprocess.run(
                ["notify-send", "--app-name", "Continuity Clipboard", "--expire-time", "4000",
                 "Clipboard received from iPhone", preview],
                timeout=SUBPROCESS_TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as error:
            log(f"notify-send failed: {error}")


SETUP_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Continuity Clipboard</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 24px;
         background: #101014; color: #f2f2f7; }
  h1 { font-size: 22px; } h2 { font-size: 17px; margin-top: 28px; }
  .card { background: #1c1c22; border-radius: 12px; padding: 16px; margin: 12px 0; }
  textarea { width: 100%; min-height: 90px; border-radius: 8px; border: 1px solid #3a3a44;
             background: #101014; color: #f2f2f7; padding: 10px; box-sizing: border-box; }
  button, .button { display: inline-block; margin-top: 10px; padding: 10px 16px; border: 0;
                    border-radius: 8px; background: #0a84ff; color: white; font-size: 16px;
                    text-decoration: none; }
  code { background: #26262e; padding: 2px 6px; border-radius: 6px; word-break: break-all; }
  ol { padding-left: 20px; } li { margin: 6px 0; }
  .dim { color: #98989f; font-size: 14px; }
  .copy { margin: 0 0 0 8px; padding: 6px 12px; border: 0; border-radius: 8px;
          background: #2c2c34; color: #f2f2f7; font-size: 14px; }
  .valuerow { display: flex; align-items: center; justify-content: space-between;
              gap: 8px; margin: 8px 0; }
  .valuerow code { flex: 1; padding: 8px; }
</style>
<script>
function copyText(value, button) {
  var label = button.textContent
  function done(ok) {
    button.textContent = ok ? "Copied \u2713" : "Long-press to copy"
    setTimeout(function() { button.textContent = label }, 1600)
  }
  function legacy() {
    var area = document.createElement("textarea")
    area.value = value
    area.setAttribute("readonly", "")
    area.style.position = "absolute"
    area.style.left = "-9999px"
    document.body.appendChild(area)
    area.select()
    area.setSelectionRange(0, value.length)
    var ok = false
    try { ok = document.execCommand("copy") } catch (error) {}
    document.body.removeChild(area)
    done(ok)
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(value).then(function() { done(true) }, legacy)
  } else {
    legacy()
  }
}
</script>
</head>
<body>
<h1>Continuity Clipboard</h1>
<p class="dim">Paired with __HOST__ &middot; bridge port __PORT__</p>

<div class="card">
  <h2 style="margin-top:0">Send text to the desktop</h2>
  <form method="post" action="/clip?token=__TOKEN__">
    <textarea name="text" placeholder="Paste here, then Send"></textarea>
    <button type="submit">Send to desktop</button>
  </form>
</div>

<div class="card">
  <h2 style="margin-top:0">View the desktop clipboard</h2>
  <p class="dim">Opens the current desktop clipboard &mdash; long-press to copy.</p>
  <a class="button" href="/clip?token=__TOKEN__">Open desktop clipboard</a>
</div>

<h2>Get the two shortcuts</h2>
__ONE_TAP__

<div class="card">
  <b>&ldquo;Send to Omarchy&rdquo;</b> &mdash; recommended version (any text &amp; images):
  <div class="valuerow"><code>__CLIP_URL__</code>
    <button class="copy" onclick="copyText('__CLIP_URL__', this)">Copy</button></div>
  <div class="valuerow"><code>__TOKEN__</code>
    <button class="copy" onclick="copyText('__TOKEN__', this)">Copy</button></div>
  <ol>
    <li>Action: <b>Get Clipboard</b></li>
    <li>Action: <b>Get Contents of URL</b> &rarr; paste the URL above</li>
    <li>Tap the small <b>&#9656; arrow on the action card</b> (&ldquo;Show More&rdquo;
        on older iOS) &mdash; it reveals the hidden rows</li>
    <li>Method &rarr; <b>POST</b></li>
    <li>Headers &rarr; add <code>X-Clip-Token</code> = paste the token above</li>
    <li>Request Body &rarr; <b>File</b> &rarr; tap the <b>File</b> slot &rarr;
        <b>Select Variable</b> &rarr; pick <b>Clipboard</b> (the slot must show the
        blue Clipboard token, not stay empty)</li>
  </ol>
  <p class="dim">The clipboard rides the request <b>body</b>, so it survives spaces,
  line breaks, and long text intact &mdash; use this for seed phrases, passwords,
  code, and multi-line notes. Copied photos and screenshots arrive as HEIC and the
  desktop converts them to PNG automatically, so they paste anywhere.</p>
  <a class="button" href="shortcuts://create-shortcut">Open Shortcuts editor</a>
</div>

<div class="card">
  <b>&ldquo;Send to Omarchy&rdquo;</b> &mdash; quick one-action version (short single-line text only):
  <div class="valuerow"><code>__CLIP_URL__?token=__TOKEN__&amp;text=</code>
    <button class="copy" onclick="copyText('__CLIP_URL__?token=__TOKEN__&text=', this)">Copy</button></div>
  <ol>
    <li>Action: <b>Get Contents of URL</b> &rarr; paste the URL above into the URL field</li>
    <li>With the cursor at the very end (after <code>text=</code>), tap
        <b>Clipboard</b> in the variable bar above the keyboard — it must
        appear as a <b>blue pill</b>. Typing the word “Clipboard” sends the
        word itself</li>
  </ol>
  <p class="dim">&#9888;&#65039; <b>Warning:</b> the clipboard rides the URL here, and
  Shortcuts does <b>not</b> reliably percent-encode it. Any <b>space or line break</b>
  truncates the URL &mdash; a 24-word seed phrase arrives as just its first word, and
  multi-line text is cut at the first newline. Use this only for short single-line
  values (a link, a code with no spaces). For anything else, use the recommended
  POST version above.</p>
  <a class="button" href="shortcuts://create-shortcut">Open Shortcuts editor</a>
</div>

<div class="card">
  <b>Bonus: send photos straight from the Share Sheet</b>
  <ol>
    <li>Duplicate the recommended (POST) version; open its details (<b>&#9432;</b>) and enable
        <b>Show in Share Sheet</b> (accept Images and Text)</li>
    <li>Change Request Body &rarr; File to the <b>Shortcut Input</b> variable</li>
  </ol>
  <p class="dim">Then in Photos or any app: Share &rarr; <b>Send to Omarchy</b> &mdash;
  no copying needed.</p>
</div>

<div class="card">
  <b>&ldquo;Get from Omarchy&rdquo;</b> &mdash; two actions:
  <div class="valuerow"><code>__CLIP_URL__?token=__TOKEN__</code>
    <button class="copy" onclick="copyText('__CLIP_URL__?token=__TOKEN__', this)">Copy</button></div>
  <ol>
    <li>Action: <b>Get Contents of URL</b> &rarr; paste the URL above</li>
    <li>Action: <b>Copy to Clipboard</b></li>
  </ol>
  <a class="button" href="shortcuts://create-shortcut">Open Shortcuts editor</a>
</div>

<h2>Make it feel like Universal Clipboard</h2>
<p class="dim">iOS only lets Apple's own software read the clipboard silently in the
background, so a one-time gesture stands in for the Apple magic. These
automations run instantly, with no confirmation and no app opening:</p>

<div class="card">
  <b>Back Tap / Action button &mdash; recommended</b>
  <ol>
    <li>Settings &rarr; Accessibility &rarr; Touch &rarr; <b>Back Tap</b> &rarr;
        Double Tap &rarr; <b>Send to Omarchy</b></li>
    <li>Or (iPhone 15 Pro and later): Settings &rarr; <b>Action Button</b> &rarr;
        Shortcut &rarr; <b>Send to Omarchy</b></li>
  </ol>
  <p class="dim">Copy anywhere &rarr; double-tap the back of the phone &rarr;
  paste on the desktop. No screen, no site, no app switch.</p>
</div>

<div class="card">
  <b>Tap-to-beam with an NFC tag</b>
  <ol>
    <li>Stick a cheap NFC sticker on your MacBook's palm rest</li>
    <li>Shortcuts &rarr; Automation &rarr; New &rarr; <b>NFC</b> &rarr; scan the tag
        &rarr; run <b>Send to Omarchy</b> &rarr; <b>Run Immediately</b></li>
  </ol>
  <p class="dim">Copy &rarr; tap the phone on the laptop &rarr; paste on the
  desktop. The AirDrop feel, without the screen.</p>
</div>

<div class="card">
  <b>Auto-pull the desktop clipboard (zero taps)</b>
  <ol>
    <li>Shortcuts &rarr; Automation &rarr; New &rarr; <b>App</b> &rarr; choose the
        apps you paste into (Notes, Safari, Messages&hellip;) &rarr; <b>Is Opened</b></li>
    <li>Run <b>Get from Omarchy</b> &rarr; <b>Run Immediately</b></li>
  </ol>
  <p class="dim">Copy on the desktop, open the app on the phone, paste &mdash;
  it is already there.</p>
</div>

<p class="dim">Why a gesture at all? Apple's Universal Clipboard rides private
entitlements: its own background daemon serves the iPhone clipboard over AWDL
to devices on the same iCloud account. Third-party software cannot read the
iOS clipboard in the background &mdash; a deliberate iOS security rule &mdash; so a
silent gesture is the honest minimum on any non-Apple receiver.</p>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP}/{VERSION}"
    protocol_version = "HTTP/1.1"

    @property
    def bridge(self):
        return self.server.bridge

    def log_message(self, fmt, *args):
        log("http " + (fmt % args))

    def _respond(self, code, body=b"", content_type=None, location=None):
        self.send_response(code)
        if location:
            self.send_header("Location", location)
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, code, payload):
        self._respond(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _authorized(self):
        supplied = self.headers.get("X-Clip-Token", "")
        if not supplied:
            supplied = (parse_qs(urlsplit(self.path).query).get("token") or [""])[0]
        return hmac.compare_digest(supplied, self.bridge.token)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/ping":
            self._json(200, {"ok": True, "app": APP, "version": VERSION})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "missing or invalid token"})
            return
        if path == "/":
            self._respond(302, location=f"/setup?token={self.bridge.token}")
            return
        if path == "/setup":
            self._respond(200, self._setup_page(), "text/html; charset=utf-8")
            return
        if path == "/clip":
            # The one-action shortcut rides the clipboard in the query string:
            # GET /clip?token=...&text=<Clipboard>. Presence of `text` makes
            # this a send, not a fetch.
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if "text" in query:
                self._handle_text_send((query.get("text") or [""])[0])
                return
            # A GET carrying a body is a send from a shortcut whose author
            # never found the (hidden) Method selector. Honor the intent.
            try:
                pending = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                pending = 0
            if pending > 0:
                self._handle_send()
                return
            try:
                mime, data = self.bridge.read_clip()
            except (OSError, subprocess.SubprocessError) as error:
                log(f"wl-paste failed: {error}")
                self._json(500, {"ok": False, "error": "could not read the desktop clipboard"})
                return
            if mime is None:
                self._respond(204)
                return
            self.bridge.record("desktop-to-phone", mime, data)
            self._respond(200, data, mime)
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if urlsplit(self.path).path != "/clip":
            self._json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._json(401, {"ok": False, "error": "missing or invalid token"})
            return
        self._handle_send()

    # Shortcuts' method picker lists PUT and PATCH beside POST; accept them
    # all so a stray pick still lands the clip.
    do_PUT = do_POST
    do_PATCH = do_POST

    def _handle_text_send(self, text):
        data = text.encode("utf-8")
        if not data.strip():
            self.bridge.notify("Received an empty clip — the shortcut sent no text")
            self._json(400, {"ok": False, "error": "empty text parameter"})
            return
        try:
            self.bridge.write_clip("text/plain", data)
        except (OSError, subprocess.SubprocessError) as error:
            log(f"wl-copy failed: {error}")
            self._json(500, {"ok": False, "error": "could not write the desktop clipboard"})
            return
        self.bridge.record("phone-to-desktop", "text/plain", data)
        if text.strip() in LITERAL_VARIABLE_NAMES:
            self.bridge.notify(
                f'Received the literal word "{text.strip()}" — in the shortcut, delete it and'
                " insert the blue variable token from the bar above the keyboard instead")
            self._json(200, {"ok": True, "hint": "literal variable name received; insert the blue variable token, do not type the word"})
            return
        self.bridge.notify(preview_for("text/plain", data))
        self._json(200, {"ok": True})

    def _handle_send(self):
        # Real shortcuts often end up as hybrids of the recipes: a POST with
        # the clipboard riding the ?text= query. A genuine query value wins
        # on any method; a typed-literal variable name there is ignored so
        # it cannot shadow the body (e.g. an image send).
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        qtext = (query.get("text") or [""])[0]
        if qtext.strip() and qtext.strip() not in LITERAL_VARIABLE_NAMES:
            self._handle_text_send(qtext)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            if qtext.strip() in LITERAL_VARIABLE_NAMES:
                self.bridge.notify(
                    f'Received the literal word "{qtext.strip()}" — in the shortcut URL, delete it'
                    " and insert the blue Clipboard variable from the bar above the keyboard")
                self._json(200, {"ok": False, "hint": "literal variable name received; insert the blue variable token, do not type the word"})
            else:
                self._json(400, {"ok": False, "error": "empty body"})
            return
        if length > MAX_BODY_BYTES:
            self._json(413, {"ok": False, "error": "clip larger than the 10 MiB limit"})
            return
        data = self.rfile.read(length)
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()

        wants_html = content_type == "application/x-www-form-urlencoded"
        if wants_html:
            fields = parse_qs(data.decode("utf-8", "replace"))
            data = (fields.get("text") or [""])[0].encode("utf-8")
            content_type = "text/plain"
            if not data:
                self._json(400, {"ok": False, "error": "empty body"})
                return

        if content_type.startswith("text/"):
            mime = "text/plain"
        elif content_type.startswith("image/"):
            mime = content_type
        elif content_type in ("", "application/octet-stream"):
            mime = sniff_mime(data)
        else:
            mime = None
        if mime is None:
            self._json(415, {"ok": False, "error": f"unsupported content type: {content_type or 'unknown'}"})
            return

        if mime.startswith("image/"):
            mime, data = normalize_image(mime, data)
        if mime == "text/plain" and not data.strip():
            # An "empty" text clip is a misconfigured shortcut (the Request
            # Body File slot not bound to the Clipboard variable), not a real
            # clip. Say so instead of silently blanking the clipboard.
            self.bridge.notify("Received an empty clip — bind Request Body → File to the Clipboard variable")
            self._json(422, {"ok": False, "error": "empty clip; bind the request body to the Clipboard variable"})
            return
        try:
            self.bridge.write_clip(mime, data)
        except (OSError, subprocess.SubprocessError) as error:
            log(f"wl-copy failed: {error}")
            self._json(500, {"ok": False, "error": "could not write the desktop clipboard"})
            return
        self.bridge.record("phone-to-desktop", mime, data)
        if mime == "text/plain" and data.decode("utf-8", "replace").strip() in LITERAL_VARIABLE_NAMES:
            literal = data.decode("utf-8", "replace").strip()
            self.bridge.notify(
                f'Received the literal word "{literal}" — in the shortcut, the Request Body slot'
                " must hold the blue variable token (tap Select Variable), not the typed word")
            self._json(200, {"ok": True, "hint": "literal variable name received; insert the blue variable token, do not type the word"})
            return
        self.bridge.notify(preview_for(mime, data))

        if wants_html:
            body = ("<!DOCTYPE html><meta charset=utf-8>"
                    "<meta name=viewport content='width=device-width, initial-scale=1'>"
                    "<body style='font-family:system-ui;background:#101014;color:#f2f2f7;"
                    "padding:40px;text-align:center'><h2>Sent to the desktop &#10003;</h2>"
                    "<p><a style='color:#0a84ff' href='javascript:history.back()'>&larr; Back</a></p>"
                    ).encode("utf-8")
            self._respond(200, body, "text/html; charset=utf-8")
        else:
            self._json(200, {"ok": True})

    def _setup_page(self):
        bridge = self.bridge
        base = f"http://{bridge.pair_host}:{bridge.port}"
        links = load_shortcut_links(bridge.state_dir)
        if "send" in links and "get" in links:
            one_tap = (
                '<div class="card">'
                '<b>One-tap install</b>'
                '<p class="dim">Add both, then paste the token when the import asks for it:</p>'
                '<div class="valuerow"><code>__TOKEN__</code>'
                '<button class="copy" onclick="copyText(\'__TOKEN__\', this)">Copy</button></div>'
                f'<p><a class="button" href="{links["send"]}">Add &ldquo;Send to Omarchy&rdquo;</a></p>'
                f'<p><a class="button" href="{links["get"]}">Add &ldquo;Get from Omarchy&rdquo;</a></p>'
                '<p class="dim">Prefer to build them yourself? The recipes below still work.</p>'
                '</div>')
        else:
            one_tap = (
                '<p class="dim">Tip for one-tap installs: after building the two shortcuts'
                ' once, share each as an iCloud link and drop the links into'
                ' <code>~/.local/state/omarchy/continuity-clipboard/shortcut-links.json</code>'
                ' on the desktop &mdash; this page then grows Add-Shortcut buttons.'
                ' Apple only imports Apple-signed shortcut files, so an iCloud link'
                ' (signed by Apple when you share) is the only one-tap channel. See the README.</p>')
        page = (SETUP_PAGE
                .replace("__ONE_TAP__", one_tap)
                .replace("__HOST__", bridge.pair_host)
                .replace("__PORT__", str(bridge.port))
                .replace("__TOKEN__", bridge.token)
                .replace("__CLIP_URL__", base + "/clip"))
        return page.encode("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Continuity Clipboard bridge daemon")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("OMARCHY_CONTINUITY_CLIP_PORT") or DEFAULT_PORT))
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--host", default=os.environ.get("OMARCHY_CONTINUITY_CLIP_HOST") or None,
                        help="Override the pairing host (e.g. a Tailscale IP or custom name). "
                             "Default: a resolvable <hostname>.local, else the routed LAN IP.")
    parser.add_argument("--state-dir", default=default_state_dir())
    args = parser.parse_args(argv)

    os.makedirs(args.state_dir, exist_ok=True)

    # Exit code 3 means "do not retry": the bridge is owned by a process this
    # daemon must not kill, so restarting cannot help. Service.qml treats it as
    # fatal instead of looping on a port it will never win.
    lock = claim_singleton(args.state_dir)
    if lock is None:
        return 3

    if not terminate_sibling_daemons():
        return 3

    server, error = bind_server(args.bind, args.port)
    if server is None:
        log(f"could not bind {args.bind}:{args.port}: {error}")
        return 3 if isinstance(error, OSError) and error.errno == errno.EADDRINUSE else 1
    server.daemon_threads = True
    server.bridge = Bridge(args.state_dir, server.server_address[1], host_override=args.host)
    server.bridge.write_status()
    log(f"listening on {args.bind}:{server.bridge.port} (state: {args.state_dir})")

    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
