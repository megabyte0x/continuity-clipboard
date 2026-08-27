import QtQuick
import Quickshell
import qs.Ui

// Bar entry point for Continuity Clipboard: a phone glyph that dims while
// the bridge daemon is down, and hosts the pairing/status panel.
BarWidget {
  id: root
  moduleName: "continuity-clipboard"

  readonly property var bridgeService: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("continuity-clipboard")
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
