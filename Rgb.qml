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
  property int screenBrightness: 100
  property string lcdTheme: "grid"
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

  // Re-read the real state whenever the popup opens: the theme hook or another
  // terminal session may have changed it since the widget last looked.
  onOpenedChanged: if (root.opened) root.refresh()

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
          root.screenBrightness = state.lcd === undefined ? 100 : Number(state.lcd)
          root.lcdTheme = state.lcdTheme === undefined ? "grid" : String(state.lcdTheme)
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

  // Brightness is pushed while the slider moves, throttled: the CLI applies the
  // visible targets in tens of milliseconds, so the light follows the hand
  // instead of waiting for the drag to end. The board lags in the background,
  // which is why the state is not re-read on every step.
  Timer {
    id: liveApply
    interval: 250
    repeat: false
    onTriggered: if (slider.dragging) root.pushBrightness(root.brightness, true)
  }

  function pushBrightness(percent, live) {
    percent = Math.max(0, Math.min(100, Math.round(percent)))
    if (!live) {
      lightsOn = percent > 0
      if (percent > 0) brightness = percent
    }
    run([root.cli, "set", String(percent)])
  }

  // The cooler's screen has its own backlight and its own slider: it is a
  // different device from the lights, and dimming the strips should not dim it.
  Timer {
    id: liveScreenApply
    interval: 250
    repeat: false
    onTriggered: if (screenSlider.dragging)
      run([root.cli, "lcd", String(root.screenBrightness)])
  }

  function pushScreen(percent, live) {
    percent = Math.max(0, Math.min(100, Math.round(percent)))
    if (!live) screenBrightness = percent
    run([root.cli, "lcd", String(percent)])
  }

  // Layouts for the cooler's screen: these are the ids build by bin/lcd_themes.py,
  // which rebuilds them from the palette of whatever theme is active.
  readonly property var lcdThemes: [
    { id: "grid", label: "Сетка" },
    { id: "large", label: "Крупно" },
    { id: "bars", label: "Полосы" },
    { id: "gauges", label: "Приборы" }
  ]

  function pushTheme(id) {
    if (!id || id === lcdTheme) return
    lcdTheme = id
    run([root.cli, "lcdtheme", id], function() { refresh() })
  }

  function refresh() {
    if (statusProcess.running) return
    statusProcess.running = true
  }

  function toggleAll() {
    lightsOn = !lightsOn
    run([root.cli, lightsOn ? "on" : "off"], function() { refresh() })
  }

  function nudge(delta) {
    if (!lightsOn && delta > 0) { pushBrightness(brightness, false); return }
    pushBrightness(brightness + delta, false)
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

          onMoved: function(value) {
            root.brightness = Math.round(value)
            if (value > 0) root.lightsOn = true
            liveApply.restart()
          }
          onReleased: function(value) {
            liveApply.stop()
            root.pushBrightness(value, false)
          }
          onRightClicked: root.toggleAll()
        }

        PanelSeparator { foreground: root.textColor }

        RowLayout {
          width: parent.width
          spacing: Style.spacing.controlGap

          PanelSectionHeader {
            text: "Screen"
            foreground: root.textColor
          }

          Item { Layout.fillWidth: true }

          Text {
            text: root.screenBrightness + "%"
            color: root.dimText
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            textFormat: Text.PlainText
          }
        }

        PanelSeparator { foreground: root.textColor }

        Row {
          id: themeRow
          width: parent.width
          spacing: Style.spacing.controlGap

          Repeater {
            model: root.lcdThemes

            Button {
              text: modelData.label
              selected: root.lcdTheme === modelData.id
              hasCursor: true
              foreground: root.textColor
              onClicked: root.pushTheme(modelData.id)
            }
          }
        }

        PanelSlider {
          id: screenSlider
          bar: root.bar
          width: parent.width
          minimum: 0
          maximum: 100
          step: 1
          integer: true
          value: root.screenBrightness

          onMoved: function(value) {
            root.screenBrightness = Math.round(value)
            liveScreenApply.restart()
          }
          onReleased: function(value) {
            liveScreenApply.stop()
            root.pushScreen(value, false)
          }
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
          text: "Lights, pump, strips and the case panel. The cooler's screen has its own backlight above. RAM is left alone."
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
