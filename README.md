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

`openrgb` re-detects the whole machine on every run — about three seconds per
device — which is far too slow to sit behind a slider. Two paths fix that:

- **The board.** `bin/openrgb-fast.py` speaks the OpenRGB SDK protocol straight
  to a background `openrgb --server`, so a colour change is a single packet
  (~15 ms) instead of a full scan. The server is started on demand — the first
  change after a boot falls back to the slow path in the background and warms the
  server up, so everything after it is immediate.
- **Lian Li.** Brightness goes over the daemon's socket. The guard that a write
  needs is cached on disk, the cooler screen's backlight rides in the same
  process, and the command stops waiting after 150 ms: the daemon sometimes holds
  a queue while it uploads frames to the wireless strips, and the interface must
  not wait for that.

| Target | Time to apply |
|---|---|
| Lian Li devices (socket) | ~60 ms |
| Motherboard and graphics card (SDK) | ~15 ms |
| Cooler screen backlight | ~60 ms |
| 8.8" panel (`bezel`) | ~15 ms |
| Whole `set` command | 70–190 ms |

Measured end to end: `omarchy-rgb set N` went from 6.3 s to 70–190 ms, an
unchanged value costs 4 ms and touches nothing, and six rapid changes collapse
into one write instead of six.

## Brightness through colour

Lian Li's firmware has five brightness levels, and the plain brightness field of
an ASRock mode is not even acknowledged by the driver (`MODE_FLAG_HAS_PER_LED_COLOR`
without the brightness flag). Both therefore dim by scaling the **colour**: the
theme accent is multiplied by the level, so 3% is really `#020706` and not "the
dimmest of five steps". Switching off sends black rather than a mode change, which
is why it is instant as well.

The accent of the current Omarchy theme is the single source of that colour, the
same one the theme hook writes. Per-zone colours set by hand in the Lian Li GUI
are replaced by it whenever the slider moves.

### Surviving a theme switch

The theme hook restarts the Lian Li daemon, which then spends up to fifteen
seconds bringing its RGB controller up. Two things followed from that, and both
are fixed:

- the widget writes through a cached `WriteGuard` and a long-lived socket; after
  a restart both are stale, so a failed write now reconnects, refreshes the guard
  and retries once, and a write that still fails is retried in the background
  until the daemon answers — but only while the value is still the current one, so
  a stale retry can never overwrite a newer level;
- the theme hook does not race the daemon at all any more. It writes the
  brightness-scaled colours and the cooler screen's level straight into the
  configuration file, so the daemon reads the finished values when it starts;
- the level itself lives in `state.json`, which the hook reads before it touches
  anything, so the new theme's colours arrive at the brightness you chose - and a
  light you switched off stays off through a theme switch;
- the motherboard and the graphics card are painted by the same code path as the
  slider, not only by the `openrgb` command line. The ASRock driver stores a
  per-LED colour with its red and blue channels swapped for Static mode, so a
  colour written by the two paths differently shows up as a different hue; going
  through the SDK keeps the theme hook and the slider in agreement.
- the live level is written to the configuration file as well as to the device.
  The cooler screen's brightness goes over IPC, which never reaches the file, and
  the file is what the daemon applies when the hook restarts it.

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
