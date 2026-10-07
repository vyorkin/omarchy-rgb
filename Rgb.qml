import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// One knob for every light on this machine: a bar glyph, a slider and a switch.
//
// The widget owns no hardware knowledge. Everything goes through
// `bin/omarchy-rgb`, which talks to OpenRGB for the motherboard and the graphics
// card, to the Lian Li daemon's Unix socket for the AIO, its fans and the
// wireless strips, and to `bezel` for the 8.8" case panel. That keeps the QML
// free of device names and lets the same script be used from a terminal.
//
// Actions: left click opens the popup, middle click toggles every light, the
// slider applies on release (one command per drag, not per pixel), Escape closes
// and the arrow keys nudge the slider by five.
Panel {
  id: root

  moduleName: "io.github.vyorkin.omarchy-rgb"
  ipcTarget: "io.github.vyorkin.omarchy-rgb"

  property bool lightsOn: true
  property int brightness: 100
  property bool busy: false
  property string errorText: ""

  // Quickshell hands out a file:// URL for the plugin directory; the CLI sits
  // next to this file, so the widget works from wherever it is installed.
  readonly property string pluginDir: decodeURIComponent(
    String(Qt.resolvedUrl(".")).replace(/^file:\/\//, "").replace(/\/$/, ""))
  readonly property string cli: root.pluginDir + "/bin/omarchy-rgb"

  readonly property color foreground: bar && bar.barForeground !== undefined
    ? bar.barForeground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color textColor: Color.popups.text
  readonly property color dimText: Qt.darker(textColor, 1.5)
  readonly property real fill: lightsOn ? Math.max(0, Math.min(1, brightness / 100)) : 0

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: refresh()

  // ------------------------------------------------------------------ backend ---

  // Status is read on demand rather than polled: nothing changes the level
  // except this widget and the theme hook, and both update it as they go.
  Process {
    id: statusProcess
    command: [root.cli, "status", "--json"]
    running: false

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var state = JSON.parse(text)
          root.lightsOn = state.on === true
          root.brightness = Number(state.brightness)
          root.errorText = ""
        } catch (error) {
          root.errorText = "cannot read state"
        }
      }
    }
  }

  // One fresh Process per command: a reused object can miss its exit event and
  // then keep reporting "running", which would wedge every later action.
  Component {
    id: commandComponent
    Process {
      property var onDone
      stderr: StdioCollector { id: commandError; waitForEnd: true }
      stdout: StdioCollector { waitForEnd: true }
      onExited: function(code) {
        root.busy = false
        root.errorText = code === 0 ? "" : String(commandError.text || "").trim()
        if (onDone) onDone(code)
        destroy()
      }
    }
  }

  function run(args, onDone) {
    var process = commandComponent.createObject(root, { command: args, onDone: onDone })
    if (!process) return
    root.busy = true
    process.running = true
  }

  function refresh() {
    if (statusProcess.running) return
    statusProcess.running = true
  }

  function applyBrightness(percent) {
    percent = Math.max(0, Math.min(100, Math.round(percent)))
    brightness = percent === 0 ? brightness : percent
    lightsOn = percent > 0
    run([root.cli, "set", String(percent)], function() { refresh() })
  }

  function toggleAll() {
    lightsOn = !lightsOn
    run([root.cli, lightsOn ? "on" : "off"], function() { refresh() })
  }

  function nudge(delta) {
    if (!lightsOn && delta > 0) { applyBrightness(brightness); return }
    applyBrightness(brightness + delta)
  }

  // ---------------------------------------------------------------------- bar ---

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    foreground: root.foreground
    tooltipText: root.lightsOn
      ? "Lighting · " + root.brightness + "% · middle click to switch off"
      : "Lighting off · middle click to switch on"

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.MiddleButton) root.toggleAll()
      else root.toggle()
    }

    // The glyph is the whole reading: a thin ring with a filled disc that grows
    // with the level. Off leaves a hollow ring, which reads as "dark" without a
    // second icon or a colour change.
    iconComponent: Component {
      Canvas {
        id: glyph
        antialiasing: true

        onPaint: {
          var ctx = getContext("2d")
          var size = Math.min(width, height)
          var centre = size / 2
          ctx.reset()

          ctx.strokeStyle = root.foreground
          ctx.lineWidth = Math.max(1, size * 0.085)
          ctx.globalAlpha = root.lightsOn ? 1.0 : 0.45
          ctx.beginPath()
          ctx.arc(centre, centre, centre - ctx.lineWidth / 2, 0, 2 * Math.PI)
          ctx.stroke()

          var radius = (centre - ctx.lineWidth) * Math.sqrt(root.fill)
          if (radius > 0.4) {
            ctx.globalAlpha = 0.35 + 0.65 * root.fill
            ctx.fillStyle = root.foreground
            ctx.beginPath()
            ctx.arc(centre, centre, radius, 0, 2 * Math.PI)
            ctx.fill()
          }
          ctx.globalAlpha = 1.0
        }

        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        Connections {
          target: root
          function onFillChanged() { glyph.requestPaint() }
          function onForegroundChanged() { glyph.requestPaint() }
          function onLightsOnChanged() { glyph.requestPaint() }
        }
      }
    }
  }

  // -------------------------------------------------------------------- popup ---

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keys
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(body.implicitHeight)

    PanelKeyCatcher {
      id: keys
      anchors.fill: parent
      onMoveRequested: function(dx, dy) { root.nudge(dy > 0 ? 5 : -5) }
      onActivateRequested: root.toggleAll()
      onCloseRequested: root.close()

      Column {
        id: body
        width: parent.width
        spacing: Style.spacing.controlGap

        RowLayout {
          width: parent.width
          spacing: Style.spacing.controlGap

          PanelSectionHeader {
            text: "Lighting"
            foreground: root.textColor
          }

          Item { Layout.fillWidth: true }

          Text {
            text: root.lightsOn ? root.brightness + "%" : "off"
            color: root.lightsOn ? root.dimText : root.accent
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            textFormat: Text.PlainText
          }

          ToggleSwitch {
            checked: root.lightsOn
            busy: root.busy
            foreground: root.textColor
            onToggled: root.toggleAll()
          }
        }

        PanelSeparator { foreground: root.textColor }

        PanelSlider {
          id: slider
          bar: root.bar
          width: parent.width
          minimum: 0
          maximum: 100
          step: 1
          integer: true
          value: root.lightsOn ? root.brightness : 0
          opacity: root.lightsOn ? 1.0 : 0.45

          onMoved: function(value) { root.brightness = Math.round(value) }
          onReleased: function(value) { root.applyBrightness(value) }
          onRightClicked: root.toggleAll()
        }

        Text {
          width: parent.width
          visible: root.errorText !== ""
          text: root.errorText
          color: Color.urgent
          wrapMode: Text.WordWrap
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          textFormat: Text.PlainText
        }

        Text {
          width: parent.width
          text: "Lights, pump, strips and both screens. RAM is left alone."
          color: root.dimText
          wrapMode: Text.WordWrap
          font.family: Style.font.family
          font.pixelSize: Style.font.bodySmall
          textFormat: Text.PlainText
        }
      }
    }
  }
}
