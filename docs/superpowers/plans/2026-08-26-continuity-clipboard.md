# Continuity Clipboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An Omarchy shell plugin that lets you copy on an iPhone and paste on an Omarchy desktop (and back), via a token-authenticated LAN bridge daemon plus one-tap iOS Shortcuts, with QR pairing from a bar-widget panel.

**Architecture:** One plugin with kinds `["service", "bar-widget"]` (the `omarchy.media` shape). `Service.qml` supervises `clipbridged.py`, a Python-stdlib HTTP daemon bridging `wl-copy`/`wl-paste` to `GET/POST /clip`. `BarWidget.qml` + `Panel.qml` show status and a QR of the `/setup` onboarding page.

**Tech Stack:** QML/Quickshell (Quattro shell, `qs.Ui`/`qs.Commons`), Python 3 stdlib, bash + `qrencode`, `wl-clipboard`, `libnotify`, node (tests only).

**Spec:** `docs/superpowers/specs/2026-08-26-continuity-clipboard-design.md`

## Global Constraints

- Plugin dir (and repo root): `~/.config/omarchy/plugins/megabyte.continuity-clipboard/`
- Plugin id: `megabyte.continuity-clipboard` (never `omarchy.*`; no symlinks in the folder)
- Default port: `8737`; override env var: `OMARCHY_CONTINUITY_CLIP_PORT`
- State dir: `$XDG_STATE_HOME/omarchy/continuity-clipboard` (fallback `~/.local/state/...`); files: `token` (0600), `status.json`
- Auth: `X-Clip-Token` header or `?token=` query; constant-time compare; `/ping` is the only unauthenticated route
- Body cap: 10 MiB; accepted POST types: `text/*`, `image/*`, `application/x-www-form-urlencoded` (`text=` field), `application/octet-stream` (sniffed)
- Runtime deps (all in base Omarchy): `python3`, `wl-copy`, `wl-paste`, `qrencode`, `notify-send`, `bash`
- QML validated with `qmllint -I "$OMARCHY_PATH/shell"`; folder validated with `omarchy plugin validate`
- `moduleName` in QML files is the plugin id
- Commit after every task (repo already has git identity configured)

---

### Task 1: Model.js — pure panel helpers

**Files:**
- Create: `Model.js`
- Test: `tests/model.test.js`

**Interfaces:**
- Produces: `parseQrMatrix(raw) -> {rows: string[], size: int}`, `baseUrl(host, port) -> string`, `setupUrl(host, port, token) -> string`, `clipUrl(host, port) -> string`, `statusLine(status, daemonRunning) -> string`, `eventLine(status) -> string`. `status` is parsed `status.json` (or null) with `host`, `port`, `last_event: {direction, kind, preview, at}`.

- [ ] **Step 1: Write the failing tests**

`tests/model.test.js`:

```js
const test = require("node:test")
const assert = require("node:assert")
const Model = require("../Model.js")

test("parseQrMatrix accepts a square 0/1 matrix", () => {
  const out = Model.parseQrMatrix("101\n010\n111\n")
  assert.equal(out.size, 3)
  assert.deepEqual(out.rows, ["101", "010", "111"])
})

test("parseQrMatrix rejects ragged matrices", () => {
  assert.equal(Model.parseQrMatrix("10\n1").size, 0)
})

test("parseQrMatrix rejects non-binary content", () => {
  assert.equal(Model.parseQrMatrix("1a\n01").size, 0)
})

test("parseQrMatrix rejects empty input", () => {
  assert.equal(Model.parseQrMatrix("").size, 0)
  assert.equal(Model.parseQrMatrix(null).size, 0)
})

test("setupUrl embeds an encoded token", () => {
  assert.equal(
    Model.setupUrl("192.168.1.8", 8737, "a+b"),
    "http://192.168.1.8:8737/setup?token=a%2Bb")
})

test("clipUrl builds the clip endpoint", () => {
  assert.equal(Model.clipUrl("10.0.0.2", 8737), "http://10.0.0.2:8737/clip")
})

test("statusLine reports a stopped bridge before anything else", () => {
  assert.equal(Model.statusLine({ host: "h", port: 1 }, false), "Bridge not running")
})

test("statusLine shows the address when running", () => {
  assert.equal(Model.statusLine({ host: "192.168.1.8", port: 8737 }, true), "http://192.168.1.8:8737")
})

test("statusLine shows starting while running without status", () => {
  assert.equal(Model.statusLine(null, true), "Bridge starting…")
})

test("eventLine has a friendly empty state", () => {
  assert.equal(Model.eventLine(null), "No clips synced yet")
  assert.equal(Model.eventLine({}), "No clips synced yet")
})

test("eventLine formats direction, preview, and time", () => {
  const line = Model.eventLine({ last_event: { direction: "phone-to-desktop", preview: '"Hi"', at: "20:31" } })
  assert.equal(line, 'iPhone → desktop: "Hi"  ·  20:31')
  const out = Model.eventLine({ last_event: { direction: "desktop-to-phone", preview: '"Yo"', at: "20:32" } })
  assert.equal(out, 'desktop → iPhone: "Yo"  ·  20:32')
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `node --test tests/model.test.js`
Expected: FAIL — cannot find module `../Model.js`

- [ ] **Step 3: Write Model.js**

```js
// Pure helpers for the Continuity Clipboard panel. No QML imports, so the
// same file loads under plain node for tests (wifiqr Model.js pattern).

// Parses make-qr.sh output: a square matrix of 0/1 rows. A malformed matrix
// returns empty rather than rendering a code that cannot scan.
function parseQrMatrix(raw) {
  var lines = String(raw || "").trim().split(/\r?\n/).filter(function(line) { return line !== "" })
  if (lines.length === 0) return { rows: [], size: 0 }

  var size = lines[0].length
  if (size !== lines.length) return { rows: [], size: 0 }

  for (var i = 0; i < lines.length; i++) {
    if (lines[i].length !== size || !/^[01]+$/.test(lines[i])) return { rows: [], size: 0 }
  }

  return { rows: lines, size: size }
}

function baseUrl(host, port) {
  return "http://" + String(host || "") + ":" + String(port)
}

// The camera-scan pairing URL. The token rides the query string because the
// iPhone camera can only open plain links.
function setupUrl(host, port, token) {
  return baseUrl(host, port) + "/setup?token=" + encodeURIComponent(String(token || ""))
}

function clipUrl(host, port) {
  return baseUrl(host, port) + "/clip"
}

// One-line bridge state for the panel header.
function statusLine(status, daemonRunning) {
  if (!daemonRunning) return "Bridge not running"
  if (status && status.host && status.port) return baseUrl(status.host, status.port)
  return "Bridge starting…"
}

// One-line description of the last synced clip, or a friendly empty state.
function eventLine(status) {
  var event = status && status.last_event ? status.last_event : null
  if (!event || !event.preview) return "No clips synced yet"
  var arrow = event.direction === "phone-to-desktop" ? "iPhone → desktop" : "desktop → iPhone"
  var at = event.at ? "  ·  " + event.at : ""
  return arrow + ": " + event.preview + at
}

if (typeof module !== "undefined") {
  module.exports = {
    parseQrMatrix: parseQrMatrix,
    baseUrl: baseUrl,
    setupUrl: setupUrl,
    clipUrl: clipUrl,
    statusLine: statusLine,
    eventLine: eventLine
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `node --test tests/model.test.js`
Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add Model.js tests/model.test.js
git commit -m "feat: pure panel helpers (QR matrix parse, URLs, status lines)"
```

---

### Task 2: make-qr.sh — payload → 0/1 matrix

**Files:**
- Create: `make-qr.sh`

**Interfaces:**
- Produces: `bash make-qr.sh <payload>` prints a square matrix, one `[01]+` row per line (consumed by `Model.parseQrMatrix` via the panel's Process).

- [ ] **Step 1: Write make-qr.sh**

```bash
#!/bin/bash

# Emit a square 0/1 QR module matrix for the payload in $1, one row per line.
# Same technique as omarchy-network-qr: qrencode's ASCII type prints two
# characters per module; collapse each pair to a single 0/1. Margin 4 is the
# spec quiet zone.

set -euo pipefail

payload=${1:-}
[[ -n $payload ]] || { echo "usage: make-qr.sh <payload>" >&2; exit 1; }

ascii=$(printf '%s' "$payload" | qrencode --type ASCII --margin 4 --output -)
while IFS= read -r line; do
  row=
  for ((column = 0; column < ${#line}; column += 2)); do
    [[ ${line:column:2} == *#* ]] && row+=1 || row+=0
  done
  printf '%s\n' "$row"
done <<<"$ascii"
```

- [ ] **Step 2: Verify the matrix is square and binary**

Run:
```bash
bash make-qr.sh "http://192.168.1.8:8737/setup?token=test" | awk '
  NR == 1 { n = length($0) }
  { if (length($0) != n || $0 !~ /^[01]+$/) exit 1; rows++ }
  END { exit !(rows == n && n > 20) }' && echo MATRIX-OK
```
Expected: `MATRIX-OK`

- [ ] **Step 3: Commit**

```bash
git add make-qr.sh
git commit -m "feat: qrencode-backed QR matrix generator"
```

---

### Task 3: clipbridged.py — the LAN bridge daemon (TDD)

**Files:**
- Create: `clipbridged.py`
- Create: `tests/bin/wl-paste`, `tests/bin/wl-copy`, `tests/bin/notify-send` (test fakes)
- Test: `tests/test_clipbridged.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: CLI `python3 clipbridged.py --port <int> --state-dir <dir> [--bind <addr>]` (Task 4 runs exactly this); HTTP routes per Global Constraints; `token` + `status.json` files in the state dir (Task 6 reads both); module-level `MAX_BODY_BYTES`, `Bridge`, `Handler`, `ThreadingHTTPServer` importable for tests.

- [ ] **Step 1: Write the test fakes**

`tests/bin/wl-paste`:
```bash
#!/bin/bash
# Test fake driven by files under $FAKE_DIR: `types` lists mime types,
# `content` holds the clipboard payload.
case "${1:-}" in
  --list-types) cat "$FAKE_DIR/types" 2>/dev/null || true ;;
  *) cat "$FAKE_DIR/content" 2>/dev/null || true ;;
esac
exit 0
```

`tests/bin/wl-copy`:
```bash
#!/bin/bash
# Test fake: record argv and stdin so tests can assert on both.
printf '%s\n' "$*" >"$FAKE_DIR/wl-copy.args"
cat >"$FAKE_DIR/wl-copy.data"
exit 0
```

`tests/bin/notify-send`:
```bash
#!/bin/bash
printf '%s\n' "$*" >>"$FAKE_DIR/notify-send.log"
exit 0
```

Then: `chmod +x tests/bin/wl-paste tests/bin/wl-copy tests/bin/notify-send`

- [ ] **Step 2: Write the failing test suite**

`tests/test_clipbridged.py`:

```python
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PLUGIN_DIR)

import clipbridged


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BridgeServerTest(unittest.TestCase):
    def setUp(self):
        self.fake_dir = tempfile.mkdtemp(prefix="clipbridge-fake-")
        self.state_dir = tempfile.mkdtemp(prefix="clipbridge-state-")
        os.environ["FAKE_DIR"] = self.fake_dir
        self._old_path = os.environ["PATH"]
        os.environ["PATH"] = os.path.join(PLUGIN_DIR, "tests", "bin") + os.pathsep + self._old_path
        self._old_max = clipbridged.MAX_BODY_BYTES
        self.server = clipbridged.ThreadingHTTPServer(("127.0.0.1", 0), clipbridged.Handler)
        self.server.daemon_threads = True
        self.server.bridge = clipbridged.Bridge(self.state_dir, self.server.server_address[1])
        self.server.bridge.write_status()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.environ["PATH"] = self._old_path
        clipbridged.MAX_BODY_BYTES = self._old_max
        shutil.rmtree(self.fake_dir, ignore_errors=True)
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def request(self, path, data=None, headers=None, method=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def set_clip(self, types, content=b""):
        with open(os.path.join(self.fake_dir, "types"), "w") as handle:
            handle.write("".join(t + "\n" for t in types))
        with open(os.path.join(self.fake_dir, "content"), "wb") as handle:
            handle.write(content)

    def auth(self):
        return {"X-Clip-Token": self.server.bridge.token}

    # -- auth ----------------------------------------------------------

    def test_ping_needs_no_token(self):
        status, _, body = self.request("/ping")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_missing_token_rejected(self):
        status, _, _ = self.request("/clip")
        self.assertEqual(status, 401)

    def test_bad_token_rejected(self):
        status, _, _ = self.request("/clip", headers={"X-Clip-Token": "wrong"})
        self.assertEqual(status, 401)

    def test_query_token_accepted(self):
        self.set_clip(["text/plain"], b"hello")
        status, _, _ = self.request(f"/clip?token={self.server.bridge.token}")
        self.assertEqual(status, 200)

    # -- desktop -> phone ----------------------------------------------

    def test_get_text_clip(self):
        self.set_clip(["text/plain;charset=utf-8", "UTF8_STRING"], "héllo".encode())
        status, headers, body = self.request("/clip", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertEqual(body.decode(), "héllo")

    def test_get_empty_clip_is_204(self):
        self.set_clip([])
        status, _, body = self.request("/clip", headers=self.auth())
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_get_image_clip(self):
        png = b"\x89PNG\r\n\x1a\nfakepixels"
        self.set_clip(["image/png", "text/html"], png)
        status, headers, body = self.request("/clip", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body, png)

    # -- phone -> desktop ----------------------------------------------

    def test_post_text_sets_clipboard(self):
        status, _, body = self.request(
            "/clip", data="hi from phone".encode(), method="POST",
            headers={**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"hi from phone")
        with open(os.path.join(self.fake_dir, "notify-send.log")) as handle:
            self.assertIn("hi from phone", handle.read())

    def test_post_image_sets_typed_clipboard(self):
        png = b"\x89PNG\r\n\x1a\nfakepixels"
        status, _, _ = self.request(
            "/clip", data=png, method="POST",
            headers={**self.auth(), "Content-Type": "image/png"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), png)

    def test_post_form_encoded_text(self):
        status, headers, _ = self.request(
            "/clip", data=b"text=from+safari%21", method="POST",
            headers={**self.auth(), "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"from safari!")

    def test_post_octet_stream_sniffs_text(self):
        status, _, _ = self.request(
            "/clip", data=b"plain words", method="POST",
            headers={**self.auth(), "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.data"), "rb") as handle:
            self.assertEqual(handle.read(), b"plain words")

    def test_post_octet_stream_sniffs_png(self):
        png = b"\x89PNG\r\n\x1a\nfakepixels"
        status, _, _ = self.request(
            "/clip", data=png, method="POST",
            headers={**self.auth(), "Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200)
        with open(os.path.join(self.fake_dir, "wl-copy.args")) as handle:
            self.assertIn("--type image/png", handle.read())

    def test_post_too_large_is_413(self):
        clipbridged.MAX_BODY_BYTES = 16
        status, _, _ = self.request(
            "/clip", data=b"x" * 64, method="POST",
            headers={**self.auth(), "Content-Type": "text/plain"})
        self.assertEqual(status, 413)

    def test_post_unsupported_type_is_415(self):
        status, _, _ = self.request(
            "/clip", data=b"\x00\x01\x02", method="POST",
            headers={**self.auth(), "Content-Type": "application/zip"})
        self.assertEqual(status, 415)

    # -- state files ----------------------------------------------------

    def test_token_file_created_private(self):
        path = os.path.join(self.state_dir, "token")
        self.assertTrue(os.path.exists(path))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertGreater(len(self.server.bridge.token), 20)

    def test_status_json_counts_serves(self):
        self.set_clip(["text/plain"], b"payload")
        self.request("/clip", headers=self.auth())
        with open(os.path.join(self.state_dir, "status.json")) as handle:
            status = json.load(handle)
        self.assertEqual(status["served"], 1)
        self.assertEqual(status["port"], self.server.server_address[1])
        self.assertEqual(status["last_event"]["direction"], "desktop-to-phone")
        self.assertIn("payload", status["last_event"]["preview"])

    # -- pages -----------------------------------------------------------

    def test_setup_page_contains_pairing_urls(self):
        status, headers, body = self.request("/setup", headers=self.auth())
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        page = body.decode()
        self.assertIn("/clip", page)
        self.assertIn(self.server.bridge.token, page)
        self.assertIn("X-Clip-Token", page)

    def test_root_redirects_to_setup(self):
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(self.base + "/", headers=self.auth())
        try:
            response = opener.open(req, timeout=5)
            status, location = response.status, response.headers.get("Location", "")
        except urllib.error.HTTPError as error:
            status, location = error.code, error.headers.get("Location", "")
        self.assertEqual(status, 302)
        self.assertIn("/setup", location)

    def test_unknown_route_404(self):
        status, _, _ = self.request("/nope", headers=self.auth())
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v 2>&1 | tail -5`
Expected: FAIL — `ModuleNotFoundError: No module named 'clipbridged'`

- [ ] **Step 4: Write clipbridged.py**

The complete daemon (module constants first, then helpers, `Bridge`, `Handler`, `main`):

```python
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
import hmac
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

APP = "continuity-clipboard"
VERSION = "1.0.0"
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


def preview_for(mime, data):
    """Human line for notifications and status.json; never the full payload."""
    if mime.startswith("image/"):
        return f"image ({mime}, {max(1, len(data) // 1024)} KB)"
    text = data.decode("utf-8", "replace").strip().replace("\n", " ")
    if len(text) > PREVIEW_CHARS:
        text = text[: PREVIEW_CHARS - 1] + "…"
    return f'"{text}"'


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
    if b"\x00" not in data:
        try:
            data.decode("utf-8")
            return "text/plain"
        except UnicodeDecodeError:
            pass
    return None


class Bridge:
    """Daemon state plus the wl-clipboard boundary."""

    def __init__(self, state_dir, port):
        self.state_dir = state_dir
        self.port = port
        self.token = load_or_create_token(os.path.join(state_dir, "token"))
        self.host = lan_host()
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self.received = 0
        self.served = 0
        self.last_event = None
        self._lock = threading.Lock()

    def write_status(self):
        status = {
            "ok": True,
            "app": APP,
            "version": VERSION,
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "started_at": self.started_at,
            "received": self.received,
            "served": self.served,
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
            self.host = lan_host()
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
</style>
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

<h2>One-tap sync with Shortcuts</h2>
<p class="dim">Build these two shortcuts once in the Shortcuts app; run them from the
Share Sheet, Home Screen, Action button, or Back Tap.</p>

<div class="card">
  <b>&ldquo;Send to Omarchy&rdquo;</b>
  <ol>
    <li>Action: <b>Get Clipboard</b></li>
    <li>Action: <b>Get Contents of URL</b> &rarr; <code>__CLIP_URL__</code></li>
    <li>Method <b>POST</b> &rarr; Request Body <b>File</b> &rarr; choose <b>Clipboard</b></li>
    <li>Add Header: <code>X-Clip-Token</code> = <code>__TOKEN__</code></li>
  </ol>
</div>

<div class="card">
  <b>&ldquo;Get from Omarchy&rdquo;</b>
  <ol>
    <li>Action: <b>Get Contents of URL</b> &rarr; <code>__CLIP_URL__?token=__TOKEN__</code></li>
    <li>Action: <b>Copy to Clipboard</b></li>
  </ol>
</div>

<p class="dim">Copy on iPhone &rarr; run &ldquo;Send to Omarchy&rdquo; &rarr; paste on the desktop.
Copy on the desktop &rarr; run &ldquo;Get from Omarchy&rdquo; &rarr; paste anywhere on iPhone.
Apple does not let non-Apple devices join Universal Clipboard, and iOS only reads
the clipboard from a foreground action &mdash; one tap on the phone is the platform minimum.</p>
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
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
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

        try:
            self.bridge.write_clip(mime, data)
        except (OSError, subprocess.SubprocessError) as error:
            log(f"wl-copy failed: {error}")
            self._json(500, {"ok": False, "error": "could not write the desktop clipboard"})
            return
        self.bridge.record("phone-to-desktop", mime, data)
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
        base = f"http://{bridge.host}:{bridge.port}"
        page = (SETUP_PAGE
                .replace("__HOST__", bridge.host)
                .replace("__PORT__", str(bridge.port))
                .replace("__TOKEN__", bridge.token)
                .replace("__CLIP_URL__", base + "/clip"))
        return page.encode("utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Continuity Clipboard bridge daemon")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("OMARCHY_CONTINUITY_CLIP_PORT") or DEFAULT_PORT))
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--state-dir", default=default_state_dir())
    args = parser.parse_args(argv)

    os.makedirs(args.state_dir, exist_ok=True)
    try:
        server = ThreadingHTTPServer((args.bind, args.port), Handler)
    except OSError as error:
        log(f"could not bind {args.bind}:{args.port}: {error}")
        return 1
    server.daemon_threads = True
    server.bridge = Bridge(args.state_dir, server.server_address[1])
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: all tests pass (node test file is ignored by unittest discovery)

- [ ] **Step 6: Commit**

```bash
git add clipbridged.py tests/
git commit -m "feat: LAN clipboard bridge daemon with token auth and setup page"
```

---

### Task 4: Service.qml — daemon supervisor

**Files:**
- Create: `Service.qml`

**Interfaces:**
- Consumes: `clipbridged.py` CLI from Task 3.
- Produces (read via `shell.serviceFor("megabyte.continuity-clipboard")` in Tasks 5–6): `daemonRunning: bool`, `port: int`, `stateDir: string`, `restartDaemon()`, `regenerateToken()`.

- [ ] **Step 1: Write Service.qml**

```qml
import QtQuick
import Quickshell
import Quickshell.Io

// Headless supervisor for the Continuity Clipboard bridge daemon
// (clipbridged.py). The shell mounts this service while the plugin is
// enabled; disabling or removing the plugin destroys it, which stops the
// daemon with it. Crashes restart with backoff so a broken environment
// cannot hot-loop the shell.
Item {
  id: root

  // Injected by the shell's service loader.
  property var shell
  property var manifest

  readonly property string daemonScript: decodeURIComponent(
    Qt.resolvedUrl("clipbridged.py").toString().replace(/^file:\/\//, ""))

  readonly property string stateDir: {
    var xdg = Quickshell.env("XDG_STATE_HOME")
    var base = xdg && String(xdg).length > 0 ? String(xdg) : Quickshell.env("HOME") + "/.local/state"
    return base + "/omarchy/continuity-clipboard"
  }

  // 8737 unless OMARCHY_CONTINUITY_CLIP_PORT overrides it. The daemon reads
  // the same variable, but passing --port keeps the two in step even if the
  // shell and daemon see different environments.
  readonly property int port: {
    var env = parseInt(String(Quickshell.env("OMARCHY_CONTINUITY_CLIP_PORT") || ""), 10)
    return env > 0 && env < 65536 ? env : 8737
  }

  readonly property bool daemonRunning: daemon.running
  property bool restartPending: false
  property bool shuttingDown: false
  property int rapidRestarts: 0

  function restartDaemon() {
    rapidRestarts = 0
    restartTimer.stop()
    if (daemon.running) {
      restartPending = true
      daemon.running = false
    } else {
      daemon.running = true
    }
  }

  // Drops the shared secret and restarts the daemon, which mints a fresh
  // token on boot. Existing phone shortcuts stop working until re-paired --
  // that is the point of regenerating.
  function regenerateToken() {
    Quickshell.execDetached(["rm", "-f", stateDir + "/token"])
    regenTimer.restart()
  }

  Process {
    id: daemon
    command: ["python3", root.daemonScript, "--port", String(root.port), "--state-dir", root.stateDir]
    stderr: SplitParser {
      onRead: function(line) { console.log("continuity-clipboard:", String(line)) }
    }
    onStarted: uptimeTimer.restart()
    onExited: function(exitCode) {
      if (root.shuttingDown) return
      if (root.restartPending) {
        root.restartPending = false
        restartTimer.interval = 500
        restartTimer.restart()
        return
      }
      // Crash: quick retries first, then a cool-off so a machine where the
      // daemon cannot run (port taken, python missing) is not hot-looped.
      root.rapidRestarts += 1
      restartTimer.interval = root.rapidRestarts > 5 ? 30000 : 2000
      restartTimer.restart()
    }
  }

  Timer {
    id: restartTimer
    interval: 2000
    repeat: false
    onTriggered: if (!root.shuttingDown) daemon.running = true
  }

  // A daemon that stays up for a minute has escaped its crash loop.
  Timer {
    id: uptimeTimer
    interval: 60000
    repeat: false
    onTriggered: root.rapidRestarts = 0
  }

  // Give the detached rm a beat to land before the restart re-mints.
  Timer {
    id: regenTimer
    interval: 300
    repeat: false
    onTriggered: root.restartDaemon()
  }

  Component.onCompleted: daemon.running = true
  Component.onDestruction: {
    root.shuttingDown = true
    restartTimer.stop()
    daemon.running = false
  }
}
```

- [ ] **Step 2: Lint**

Run: `qmllint -I "$OMARCHY_PATH/shell" Service.qml`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add Service.qml
git commit -m "feat: service supervisor with crash backoff and token regen"
```

---

### Task 5: BarWidget.qml — bar entry point

**Files:**
- Create: `BarWidget.qml`

**Interfaces:**
- Consumes: `Service.qml` surface via `bar.shell.serviceFor(...)`; `Panel.qml` (Task 6) via `Loader` — forwards the bar panel contract (`opened/open/close/toggle/closeForPopoutSwitch/popoutSwitchClosing`) exactly as the develop-guide clock does.
- Produces: `injectPanel()` sets `panelLoader.item.bar/anchorItem/hostWidget` (Task 6's Panel declares those properties).

- [ ] **Step 1: Write BarWidget.qml**

```qml
import QtQuick
import Quickshell
import qs.Ui

// Bar entry point for Continuity Clipboard: a phone glyph that dims while
// the bridge daemon is down, and hosts the pairing/status panel.
BarWidget {
  id: root
  moduleName: "megabyte.continuity-clipboard"

  readonly property var bridgeService: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("megabyte.continuity-clipboard")
    : null
  readonly property bool bridgeRunning: bridgeService ? bridgeService.daemonRunning === true : false

  readonly property bool opened: panelLoader.item
    ? panelLoader.item.opened === true
    : false
  readonly property bool popoutSwitchClosing: panelLoader.item
    ? panelLoader.item.popoutSwitchClosing === true
    : false

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  function toggle() {
    if (panelLoader.item) panelLoader.item.toggle()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    if (!panelLoader.item) return
    panelLoader.item.bar = root.bar
    panelLoader.item.anchorItem = button
    panelLoader.item.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰄜"
    dimmed: !root.bridgeRunning
    tooltipText: root.bridgeRunning
      ? "Continuity Clipboard — bridge running"
      : "Continuity Clipboard — bridge stopped"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) root.toggle()
    }
  }
}
```

- [ ] **Step 2: Lint**

Run: `qmllint -I "$OMARCHY_PATH/shell" BarWidget.qml`
Expected: exit 0 (Panel.qml does not need to exist for lint, but Task 6 must land before the widget loads in the shell)

- [ ] **Step 3: Commit**

```bash
git add BarWidget.qml
git commit -m "feat: bar widget with bridge status glyph"
```

---

### Task 6: Panel.qml — pairing and status card

**Files:**
- Create: `Panel.qml`

**Interfaces:**
- Consumes: `Model.js` (Task 1), `make-qr.sh` (Task 2), `Service.qml` surface (`daemonRunning/port/stateDir/restartDaemon()/regenerateToken()`), state files `token` and `status.json` (Task 3), base `Panel`/`KeyboardPanel`/`PanelKeyCatcher`/`PanelActionButton` from `qs.Ui`.
- Produces: properties `bar`, `anchorItem`, `hostWidget` (set by Task 5's `injectPanel()`); base `Panel` provides `opened/toggle/close/closeForPopoutSwitch/popoutSwitchClosing`.

- [ ] **Step 1: Write Panel.qml**

```qml
import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// Pairing and status card for the Continuity Clipboard bridge: the daemon
// address, a QR of the phone setup page, the last synced clip, and inline
// actions (copy pairing link, restart bridge, regenerate token).
//
// BarWidget.qml owns the bar glyph and hands this panel the button to
// anchor against.
Panel {
  id: root
  moduleName: "megabyte.continuity-clipboard"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  readonly property var bridgeService: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("megabyte.continuity-clipboard")
    : null
  readonly property bool bridgeRunning: bridgeService ? bridgeService.daemonRunning === true : false
  readonly property int bridgePort: bridgeService && bridgeService.port ? bridgeService.port : 8737
  readonly property string stateDir: bridgeService && bridgeService.stateDir
    ? String(bridgeService.stateDir)
    : Quickshell.env("HOME") + "/.local/state/omarchy/continuity-clipboard"

  readonly property string makeQrScript: decodeURIComponent(
    Qt.resolvedUrl("make-qr.sh").toString().replace(/^file:\/\//, ""))

  property string token: ""
  property var status: null
  property var qrRows: []
  property int qrSize: 0
  property bool pendingQr: false
  property bool linkCopied: false

  readonly property string host: status && status.host ? String(status.host) : ""
  readonly property string pairUrl: host !== "" && token !== ""
    ? Model.setupUrl(host, bridgePort, token)
    : ""
  readonly property string headline: Model.statusLine(status, bridgeRunning)
  readonly property string lastSync: Model.eventLine(status)

  readonly property color contentForeground: bar ? bar.foreground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family

  function open() {
    tokenFile.reload()
    statusFile.reload()
    root.controller.show()
  }

  function close() {
    root.controller.hide()
  }

  function copySetupLink() {
    if (root.pairUrl === "") return
    Quickshell.execDetached(["wl-copy", root.pairUrl])
    root.linkCopied = true
    copiedTimer.restart()
  }

  function regenerateQr() {
    if (qrProc.running) {
      root.pendingQr = true
      return
    }
    root.qrRows = []
    root.qrSize = 0
    if (root.pairUrl === "") return
    qrProc.running = true
  }

  onPairUrlChanged: regenerateQr()

  FileView {
    id: tokenFile
    path: root.stateDir + "/token"
    watchChanges: true
    printErrors: false
    onLoaded: root.token = String(text() || "").trim()
    onFileChanged: reload()
    onLoadFailed: root.token = ""
  }

  FileView {
    id: statusFile
    path: root.stateDir + "/status.json"
    watchChanges: true
    printErrors: false
    onLoaded: {
      try { root.status = JSON.parse(text()) } catch (error) { root.status = null }
    }
    onFileChanged: reload()
    onLoadFailed: root.status = null
  }

  Process {
    id: qrProc
    command: ["bash", root.makeQrScript, root.pairUrl]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = Model.parseQrMatrix(text)
        root.qrRows = parsed.rows
        root.qrSize = parsed.size
      }
    }
    onExited: {
      if (root.pendingQr) {
        root.pendingQr = false
        Qt.callLater(root.regenerateQr)
      }
    }
  }

  Timer {
    id: copiedTimer
    interval: 1800
    repeat: false
    onTriggered: root.linkCopied = false
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(280))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Column {
        id: content
        width: parent.width
        spacing: Style.space(10)

        Text {
          width: parent.width
          text: "Continuity Clipboard"
          color: root.barForeground
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.subtitle
          font.bold: true
        }

        Text {
          width: parent.width
          text: root.headline
          color: root.bridgeRunning ? root.barForeground : "#ff6b6b"
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.bodySmall
          wrapMode: Text.WrapAnywhere
        }

        Rectangle {
          id: qrCanvas
          readonly property int moduleSize: root.qrSize > 0
            ? Math.max(2, Math.floor(Style.space(220) / root.qrSize))
            : 0

          visible: root.qrSize > 0
          width: root.qrSize * moduleSize
          height: width
          color: "white"
          radius: Style.cornerRadius
          anchors.horizontalCenter: parent.horizontalCenter

          Grid {
            anchors.centerIn: parent
            columns: root.qrSize

            Repeater {
              model: root.qrSize * root.qrSize

              Rectangle {
                required property int index
                readonly property int matrixRow: Math.floor(index / root.qrSize)
                readonly property int matrixColumn: index % root.qrSize

                width: qrCanvas.moduleSize
                height: qrCanvas.moduleSize
                color: root.qrRows[matrixRow].charAt(matrixColumn) === "1" ? "#111111" : "transparent"
              }
            }
          }
        }

        Text {
          width: parent.width
          visible: root.qrSize > 0
          text: "Scan with the iPhone camera to pair"
          color: root.barForeground
          opacity: 0.6
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.bodySmall
          horizontalAlignment: Text.AlignHCenter
        }

        Text {
          width: parent.width
          visible: root.qrSize === 0
          text: root.bridgeRunning ? "Preparing pairing code…" : "Start the bridge to pair an iPhone"
          color: root.barForeground
          opacity: 0.6
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.bodySmall
          horizontalAlignment: Text.AlignHCenter
        }

        Text {
          width: parent.width
          text: root.linkCopied ? "Pairing link copied" : root.lastSync
          color: root.barForeground
          opacity: root.linkCopied ? 1 : 0.75
          font.family: root.contentFontFamily
          font.pixelSize: Style.font.bodySmall
          elide: Text.ElideRight
        }

        Row {
          spacing: Style.space(8)
          anchors.horizontalCenter: parent.horizontalCenter

          PanelActionButton {
            iconText: "󰆏"
            tooltipText: "Copy pairing link"
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            enabled: root.pairUrl !== ""
            onClicked: root.copySetupLink()
          }

          PanelActionButton {
            iconText: "󰑐"
            tooltipText: "Restart bridge"
            foreground: root.contentForeground
            fontFamily: root.contentFontFamily
            enabled: root.bridgeService !== null
            onClicked: root.bridgeService.restartDaemon()
          }

          PanelActionButton {
            iconText: "󰌆"
            tooltipText: "Regenerate token (re-pair phones)"
            foreground: root.contentForeground
            hoverColor: "#ff6b6b"
            fontFamily: root.contentFontFamily
            enabled: root.bridgeService !== null
            onClicked: root.bridgeService.regenerateToken()
          }
        }
      }
    }
  }
}
```

- [ ] **Step 2: Lint**

Run: `qmllint -I "$OMARCHY_PATH/shell" Panel.qml`
Expected: exit 0

- [ ] **Step 3: Commit**

```bash
git add Panel.qml
git commit -m "feat: pairing panel with QR card, status, and bridge actions"
```

---

### Task 7: manifest.json + LICENSE + folder validation

**Files:**
- Create: `manifest.json`
- Create: `LICENSE`

**Interfaces:**
- Consumes: `Service.qml` (Task 4) and `BarWidget.qml` (Task 5) must exist — validate checks entry point files.
- Produces: the folder the shell discovers; `omarchy plugin validate` green.

- [ ] **Step 1: Write manifest.json**

```json
{
  "schemaVersion": 1,
  "id": "megabyte.continuity-clipboard",
  "name": "Continuity Clipboard",
  "version": "1.0.0",
  "author": "Megabyte",
  "license": "MIT",
  "description": "Copy on your iPhone, paste on your Omarchy desktop — and back. LAN clipboard bridge with one-tap iOS Shortcuts and QR pairing.",
  "kinds": ["service", "bar-widget"],
  "keepLoaded": true,
  "entryPoints": {
    "service": "Service.qml",
    "barWidget": "BarWidget.qml"
  },
  "barWidget": {
    "displayName": "Continuity Clipboard",
    "description": "iPhone ↔ desktop clipboard bridge status and pairing",
    "category": "Connectivity",
    "allowMultiple": false,
    "defaultSection": "right"
  }
}
```

- [ ] **Step 2: Write LICENSE**

```text
MIT License

Copyright (c) 2026 Megabyte

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

- [ ] **Step 3: Validate the folder and lint everything**

Run:
```bash
omarchy plugin validate "$HOME/.config/omarchy/plugins/megabyte.continuity-clipboard"
qmllint -I "$OMARCHY_PATH/shell" BarWidget.qml Panel.qml Service.qml
```
Expected: both exit 0

- [ ] **Step 4: Commit**

```bash
git add manifest.json LICENSE
git commit -m "feat: plugin manifest and license"
```

---

### Task 8: README.md

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README.md** covering, in this order: what it does; honest platform-limits note (Apple restricts Universal Clipboard to Apple devices and iOS clipboard reads to foreground actions — one tap on the phone is the minimum); install (`omarchy plugin add <repo> --enable`); iPhone pairing (open panel → scan QR → follow the setup page; the two Shortcuts recipes verbatim); usage both directions; manual Safari fallback; configuration (`OMARCHY_CONTINUITY_CLIP_PORT`); security (LAN-only trust model, token, regenerate button, 0600 file, 10 MiB cap, no TLS rationale); dependencies (`python3`, `wl-clipboard`, `qrencode`, `libnotify` — all in base Omarchy); files it writes (state dir paths); remove (`omarchy plugin remove megabyte.continuity-clipboard`). Use the finished-example README from the develop guide as the structural reference.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: README with pairing, security model, and platform limits"
```

---

### Task 9: Enable + live end-to-end acceptance

**Files:**
- Modify: `~/.config/omarchy/shell.json` (via `omarchy plugin enable` — never by hand)

- [ ] **Step 1: Discover and enable**

```bash
omarchy-shell shell rescanPlugins
omarchy plugin list --json | jq '.[] | select(.id == "megabyte.continuity-clipboard")'
omarchy plugin enable megabyte.continuity-clipboard
```
Expected: listing shows both kinds; enable exits 0; `omarchy plugin list --json` then shows `"enabled": true`.

- [ ] **Step 2: Daemon liveness**

```bash
sleep 2 && curl -sS http://127.0.0.1:8737/ping
TOKEN=$(cat ~/.local/state/omarchy/continuity-clipboard/token)
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8737/clip"        # expect 401
```
Expected: ping returns `{"ok": true, ...}`; unauthenticated clip returns 401.

- [ ] **Step 3: Round-trip both directions (the exact HTTP contract the iOS Shortcuts use)**

```bash
# phone -> desktop
curl -sS -X POST -H "X-Clip-Token: $TOKEN" -H "Content-Type: text/plain" \
  --data "e2e from fake phone" http://127.0.0.1:8737/clip
wl-paste --no-newline   # expect: e2e from fake phone

# desktop -> phone
wl-copy "e2e from desktop"
curl -sS -H "X-Clip-Token: $TOKEN" http://127.0.0.1:8737/clip   # expect: e2e from desktop

# setup page reachable on the LAN address
curl -sS "http://$(cat ~/.local/state/omarchy/continuity-clipboard/status.json | jq -r .host):8737/setup?token=$TOKEN" | grep -c "Shortcuts"
```
Expected: each read returns exactly what was posted; setup page mentions Shortcuts.

- [ ] **Step 4: Panel lifecycle through the shell**

```bash
omarchy-shell shell summon megabyte.continuity-clipboard '{}'
omarchy-shell shell hide megabyte.continuity-clipboard
qs log -p "$OMARCHY_PATH/shell" --tail 40   # no QML errors for the plugin
```
Expected: summon/hide return without `unknown`; log free of `megabyte.continuity-clipboard` errors.

- [ ] **Step 5: Teardown honors disable**

```bash
omarchy plugin disable megabyte.continuity-clipboard
sleep 2; curl -sS -m 2 http://127.0.0.1:8737/ping || echo BRIDGE-DOWN
omarchy plugin enable megabyte.continuity-clipboard
sleep 2; curl -sS http://127.0.0.1:8737/ping
```
Expected: `BRIDGE-DOWN` while disabled; ping ok again after re-enable.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: live acceptance verified" --allow-empty
```

## Self-Review Notes

- Spec coverage: routes/auth/caps (Task 3), supervisor+backoff (Task 4), bar contract (Task 5), QR panel + actions (Task 6), validation (Task 7), README honesty items (Task 8), live acceptance incl. teardown (Task 9). Clipboard history, TLS, mDNS are spec non-goals — no tasks, correct.
- Types are consistent: `daemonRunning/port/stateDir/restartDaemon/regenerateToken` names match between Tasks 4, 5, 6; `status.json` keys match between Tasks 3 and 1/6; `preview` strings match `eventLine` expectations.
- No placeholders; every code step is complete file content.
