# Continuity Clipboard — Design Spec

**Date:** 2026-08-26
**Plugin id:** `megabyte.continuity-clipboard`
**Target:** Omarchy 4.x (Quattro shell), MacBook (Apple Silicon / Asahi) or any machine running Omarchy

## 1. Problem

macOS + iPhone users get Universal Clipboard: copy on the iPhone, paste on the
Mac, and vice versa. A MacBook running Omarchy loses that. This plugin restores
the closest legitimate equivalent for an Omarchy desktop.

### Hard platform constraints (drive the whole design)

1. **Apple's Universal Clipboard is a closed protocol.** It rides Handoff
   (BLE + AWDL + iCloud pairing) between Apple-signed OSes only. No Linux
   process can join it.
2. **iOS forbids background clipboard reading by third-party code.** Only a
   foreground app or a user-triggered Shortcut may read `UIPasteboard`, so any
   iPhone→desktop path requires one user tap on the phone.
3. **Safari's `navigator.clipboard` API needs a secure context.** Plain-HTTP
   LAN pages cannot programmatically read/write the iOS clipboard, so a pure
   web app cannot be the primary mechanism. Apple **Shortcuts**' native
   "Get clipboard" / "Copy to clipboard" + "Get contents of URL" actions have
   no such restriction and work over LAN HTTP.

Therefore the achievable UX ceiling is: **one tap on the iPhone in either
direction** (Action button, Back Tap, Share Sheet, Lock-Screen/Home-Screen
widget — all can trigger a Shortcut), **zero taps on the desktop** (the bridge
daemon applies/serves the Wayland clipboard automatically). The README and the
setup page state this honestly.

## 2. Approaches considered

| Approach | Verdict |
|---|---|
| **A. LAN bridge daemon + stock iOS Shortcuts** (chosen) | Self-contained; stock iOS only; deps (`python3`, `wl-clipboard`, `qrencode`, `libnotify`, `jq`) all ship with base Omarchy; testable without a phone at the HTTP boundary. |
| B. KDE Connect wrapper | Pulls in `kdeconnectd` + an iOS app whose clipboard support is still manual-per-Apple-rules; heavy dependency for no UX gain. |
| C. Cloud relay (iCloud/webservice) | External service, privacy exposure, accounts; rejected. |

## 3. Architecture

One Omarchy shell plugin, two kinds (`omarchy.media` shape):

```
~/.config/omarchy/plugins/megabyte.continuity-clipboard/
├── manifest.json        kinds: ["service", "bar-widget"], keepLoaded: true
├── Service.qml          headless supervisor: starts/restarts/stops the daemon
├── BarWidget.qml        bar icon (clock contract: open/close/toggle/opened/…)
├── Panel.qml            status + QR pairing card + actions (KeyboardPanel)
├── Model.js             pure JS helpers (QR matrix parse, URLs, previews)
├── clipbridged.py       LAN HTTP bridge daemon (Python 3 stdlib only)
├── make-qr.sh           payload → 0/1 QR matrix via qrencode (network-qr pattern)
├── tests/               python unittest + node tests (not loaded by the shell)
├── README.md, LICENSE
```

### Data flow

**iPhone → desktop:** Shortcut "Send to Omarchy" = *Get clipboard →
Get contents of URL (POST http://IP:8737/clip, header X-Clip-Token)*. Daemon
pipes body to `wl-copy` (text or image mime), sends a `notify-send` toast,
records the event in `status.json`.

**Desktop → iPhone:** Shortcut "Get from Omarchy" = *Get contents of URL
(GET /clip) → Copy to clipboard*. Daemon snapshots the Wayland selection on
demand (`wl-paste`); images are returned with their image mime, text as UTF-8.
No clipboard watcher process is needed — on-demand reads are always current.

**Pairing:** Panel shows a QR of `http://IP:8737/setup?token=…`. Scanning it
with the iPhone camera opens the setup page: connection test, exact
pre-tokenized URLs with copy buttons, step-by-step Shortcut recipes, and a
no-shortcut manual fallback (textarea POST form to send text; a fetch link to
view desktop clipboard text for manual copy).

### Components

**`clipbridged.py`** — `ThreadingHTTPServer` on `0.0.0.0:8737` (port
overridable via `OMARCHY_CONTINUITY_CLIP_PORT`). Routes:

| Route | Auth | Behavior |
|---|---|---|
| `GET /ping` | none | `{"ok":true,"app":"continuity-clipboard","version":…}` |
| `GET /clip` | token | wl-paste snapshot → `image/*` bytes or `text/plain; charset=utf-8`; `204` when empty |
| `POST /clip` | token | body `text/*`→`wl-copy`; `image/*`→`wl-copy --type <mime>`; form-encoded `text=` field accepted (setup page); 10 MiB cap → `413` |
| `GET /setup` | token | HTML onboarding page |
| `GET /` | token | 302 → `/setup` |
| anything else | — | `404`; bad/missing token → `401` |

Token: `secrets.token_urlsafe(24)` created on first run at
`$XDG_STATE_HOME/omarchy/continuity-clipboard/token` (mode 0600); accepted via
`X-Clip-Token` header **or** `?token=` query (camera-scan needs query);
compared with `hmac.compare_digest`. After every request that changes state —
and at startup — the daemon writes `status.json` (pid, port, LAN addresses,
started_at, last_event `{direction,kind,preview,at}`, counters) atomically
(`os.replace`) so the QML side can watch one file. Text previews are truncated
to 80 chars; image events preview as `image (image/png, 123 KB)`.
`SIGTERM` shuts the server down cleanly. `notify-send` and `wl-copy` failures
are logged, never fatal to the server.

**`Service.qml`** — injected `shell`/`manifest` (flush-bar contract). Ensures
the state dir exists, then runs
`python3 clipbridged.py --port … --state-dir …` under a Quickshell `Process`.
Crash → restart with a 2 s backoff `Timer` (max 5 rapid retries, then a 30 s
cool-off so a broken environment cannot hot-loop the shell). Exposes to the
panel (via `shell.serviceFor(id)`): `daemonRunning`, `port`,
`function restartDaemon()`, `function regenerateToken()` (deletes token file,
restarts daemon; daemon mints a fresh token). Component destruction stops the
process (shell owns the lifecycle: disable/removal kills the bridge).

**`BarWidget.qml`** — `WidgetButton` with the 󰄜 glyph, tooltip shows bridge
state; left-click toggles the panel. Implements the bar contract exactly as
the develop-guide clock: `opened`, `open()`, `close()`, `toggle()`,
`closeForPopoutSwitch()`, `popoutSwitchClosing`, `injectPanel()` forwarding
`bar`/`anchorItem`/`hostWidget`. Icon renders at 45% opacity while the daemon
is down.

**`Panel.qml`** — `KeyboardPanel` anchored to the button (Esc closes via
`PanelKeyCatcher`). Content: state line ("Bridge running on
http://192.168.1.8:8737" / error), QR card (white rounded rect, Grid+Repeater
of `qrSize²` rectangles — the wifiqr renderer), last-sync line from
`status.json` (`FileView` + `watchChanges`), and three actions: Copy setup
link (`wl-copy`), Regenerate token, Restart bridge. Opening the panel (re)runs
`make-qr.sh <setup-url>` through a `Process`+`StdioCollector`; `Model.js`
parses the matrix.

**`Model.js`** — pure functions with the wifiqr `module.exports` test hook:
`parseQrMatrix(lines)`, `setupUrl(host, port, token)`, `clipUrl(host, port)`,
`statusLine(statusJson, running)`, `eventLine(statusJson)`. No QML imports —
runs under plain node for tests.

**`make-qr.sh`** — `qrencode --type ASCII --margin 4` and collapse
2-chars-per-module to `0`/`1` rows (verbatim technique from
`omarchy-network-qr`).

## 4. Security model

- Bearer token required for everything except `/ping`; constant-time compare.
- Token stored 0600 in the user state dir; regenerable from the panel.
- Server binds all interfaces so the phone can reach it; the README instructs
  use on trusted LANs and points at the token-regeneration control. No TLS:
  self-signed HTTPS breaks both iOS Shortcuts ergonomics and camera-scan
  onboarding; the threat model is a home/office LAN with a secret URL token.
- 10 MiB request cap; only `text/*` and `image/*` bodies accepted.
- Plugin runs unsandboxed inside `omarchy-shell` per platform rules: no sudo,
  no installers, no writes outside `$XDG_STATE_HOME/omarchy/continuity-clipboard`.

## 5. Error handling

- Daemon can't bind (port taken): exits non-zero with a clear stderr line;
  Service backs off and the panel shows "Bridge not running — see
  `qs log`"-style state.
- `wl-paste` empty/no selection: `204 No Content`; Shortcut shows empty result.
- Oversized/foreign content types: `413`/`415` JSON errors.
- Missing binaries (`wl-copy` etc.): `500` JSON error at use-time; daemon
  stays up.
- Shell restart / plugin disable: `Process` teardown SIGTERMs the daemon; no
  orphan (verified in acceptance tests).

## 6. Testing strategy

- **Python unittest** (`tests/test_clipbridged.py`): start the daemon on an
  ephemeral port with `PATH` pointing at `tests/bin` fakes of
  `wl-copy`/`wl-paste`/`notify-send` (record to a temp dir). Cover: ping,
  401s, GET text, GET empty→204, POST text→wl-copy call, POST image mime,
  form-encoded POST, 413 cap, token bootstrap + 0600 perms, status.json shape.
- **Node tests** (`tests/model.test.js`): matrix parsing (valid, ragged,
  non-binary), URL builders, status/event lines.
- **Static validation:** `omarchy plugin validate <dir>`, `qmllint -I
  $OMARCHY_PATH/shell` on all three QML files.
- **Live acceptance (scriptable, no phone needed):** enable plugin → daemon
  up → `curl` POST text → `wl-paste` shows it; `wl-copy` text → `curl` GET
  returns it; `/setup` renders; panel summons via
  `omarchy-shell shell summon`; disable → daemon gone. The iPhone side is
  plain HTTP GET/POST, so the curl checks exercise the exact contract the
  Shortcuts use.

## 7. Out of scope (YAGNI)

- Clipboard history (Omarchy already ships `omarchy.clipboard`).
- mDNS auto-discovery, TLS, multiple paired phones with distinct tokens,
  Android companion — all possible later; not needed for v1.
- Push-to-phone notifications (impossible without an Apple developer service).
