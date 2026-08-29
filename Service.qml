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

  // Set when the daemon reports an unwinnable environment (exit 3: the port is
  // held by a process it must not kill). Restarting cannot clear that, so the
  // supervisor stays down instead of logging a bind failure every 2 seconds.
  property bool daemonBlocked: false

  function restartDaemon() {
    rapidRestarts = 0
    daemonBlocked = false
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
      // A daemon that exited is not "up": leaving the uptime timer armed would
      // zero rapidRestarts 60s later and drop a permanently failing daemon back
      // into 2s retries forever, defeating the backoff below.
      uptimeTimer.stop()
      if (root.shuttingDown) return
      if (exitCode === 3) {
        root.daemonBlocked = true
        console.log("continuity-clipboard: bridge port unavailable; supervisor idle until the plugin is reloaded")
        return
      }
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
