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
  moduleName: "continuity-clipboard"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  readonly property var bridgeService: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("continuity-clipboard")
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

  // Prefer the stable mDNS `<hostname>.local` pairing host when the daemon
  // reports one; it survives Wi-Fi/DHCP address changes. Fall back to the raw
  // LAN IP for older daemons or when mDNS is unavailable.
  readonly property string host: status && status.pair_host ? String(status.pair_host)
    : (status && status.host ? String(status.host) : "")
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
    // Assigned imperatively, not bound: a binding can lag the pairUrl change
    // signal that triggered this call, launching with a stale URL (the
    // wifiqr panel sets command the same way for the same reason).
    qrProc.command = ["bash", root.makeQrScript, root.pairUrl]
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
