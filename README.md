# RGB Control for the Omarchy bar

One slider for the brightness of every light on the machine, and one switch that
turns all of them off. The bar shows a small ring whose filled centre grows with
the level; when everything is dark, the ring is left hollow.

![bar widget](preview.png)

## What it touches

| Target | How |
|---|---|
| Motherboard ARGB headers, graphics card | `openrgb` — mode, accent colour and `-b` percentage |
| AIO pump and fans, wireless strips and fans | the Lian Li daemon's Unix socket: one `SetRgbConfig` per change |
| Cooler screen backlight | the same socket, `SetLcdBrightness` |
| 8.8" case panel | `bezel brightness` |

Brightness on Lian Li hardware has five firmware levels, so the slider quantises
there (0, 25, 50, 75, 100) while OpenRGB and the screens take the exact value.

Deliberately out of scope: **RAM modules**. Their RGB sits behind the ENE SMBus
controller, and writing to it has a history of damaging DDR5 modules. The widget
never sends anything to them.

## Actions

| Input | Result |
|---|---|
| Left click on the bar glyph | open the popup |
| Middle click | switch every light off, or back to the last level |
| Drag the slider | the level applies on release, one command per drag |
| Right click the slider | toggle all lights |
| Arrow keys, Escape | nudge by 5, close |

## Requirements

Everything is optional — a missing tool is skipped, not fatal:

- `openrgb` for the motherboard and the graphics card;
- [lian-li-linux](https://github.com/sgtaziz/lian-li-linux) running as the user
  daemon, for the AIO, its fans and the wireless strips;
- [bezel](https://github.com/slipalison/bezel) for the 8.8" case panel;
- `omarchy-theme-color` (part of Omarchy) to read the current theme accent.

## Install

```bash
omarchy plugin install https://github.com/vyorkin/omarchy-rgb
```

Or by hand:

```bash
git clone https://github.com/vyorkin/omarchy-rgb ~/.config/omarchy/plugins/io.github.vyorkin.omarchy-rgb
omarchy restart shell
```

Then add it to the bar if the shell does not offer to:

```bash
omarchy bar move io.github.vyorkin.omarchy-rgb --section right
```

## Command line

The widget is a thin QML front end for `bin/omarchy-rgb`, which is usable on its
own:

```bash
bin/omarchy-rgb status          # lights on, 60%, accent #50dcc8
bin/omarchy-rgb status --json   # {"on":true,"brightness":60,"accent":"#50dcc8"}
bin/omarchy-rgb set 60
bin/omarchy-rgb on
bin/omarchy-rgb off
bin/omarchy-rgb toggle
bin/omarchy-rgb accent          # the accent colour of the current Omarchy theme
```

State lives in `${XDG_STATE_HOME:-~/.local/state}/omarchy-rgb/state.json`, so
`on` can restore the level you were at before switching everything off.

## Latency

`openrgb` detects the whole machine on every run — about three seconds per
device — so waiting for it made the slider feel dead. The script therefore
applies the fast targets first, in parallel, and hands the board to a detached
worker:

| Target | Time to apply |
|---|---|
| Lian Li devices (socket) | ~50 ms |
| Cooler screen backlight | ~50 ms |
| 8.8" panel (`bezel`) | ~16 ms |
| Motherboard and graphics card | a few seconds, in the background |

The worker takes a lock and reads the current state when it gets it, so a drag
collapses into one write instead of twenty. The widget pushes the level while
you drag, throttled to 120 ms, and does not re-read the state on every step.

## Configure

Device names for OpenRGB and the AIO's device ID are listed at the top of
`bin/omarchy-rgb`:

```bash
OPENRGB_DEVICES=(
    "ASRock X870 Pro RS WiFi"
    "MSI GeForce RTX 5080 Gaming Trio OC"
)
AIO_LCD_DEVICE="hid:0416:7395:8-12"
```

Run `openrgb --list-devices` to see the names OpenRGB uses on your machine. Lian
Li devices are not listed by hand: they come from the daemon's own configuration,
so strips and fans that appear later are picked up on their own.

## Notes

- Lian Li writes need a `WriteGuard`, which pins the daemon version and instance
  id. The script asks for `GetDaemonInfo` first and wraps every write, exactly as
  the Lian Li GUI does. A daemon that is older or newer than the script reports
  `Changes require a compatible client` instead of applying the level.
- The script never restarts the Lian Li daemon: brightness goes through IPC, so
  the AIO screen does not re-initialise on every slider move.
- If the Lian Li daemon is not running, only OpenRGB and the panel are touched.

## Licence

MIT.
