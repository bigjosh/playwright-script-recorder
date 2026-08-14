# Playwright Script Recorder

Record point-and-click browser scripts, then replay them through Chrome's
remote debug port. Works on regular websites and on the Chrome Remote
Desktop web client (the image comparison tolerates its video-compression
noise).

- `playwrightscriptlib.py` — small runtime library the recorded scripts use
- `playwrightscriptrecord.py` — interactive recorder that writes the scripts

## Setup

Python 3.9+ with tkinter (included in the standard Windows installer), then:

```
pip install -r requirements.txt
```

(Just `playwright` and `Pillow` — no `playwright install` needed, since we
attach to your existing Chrome.)

## 1. Start Chrome with the debug port

```
start "" "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\temp\chrome-debug
```

`chrome.exe` is usually not on PATH, so use the full path — depending on the
install it is under `C:\Program Files\...` or `C:\Program Files (x86)\...`.

The separate `--user-data-dir` is required if Chrome is already running
without the flag (otherwise the new window joins the old process and the
port never opens). Verify it works by opening `http://127.0.0.1:9222/json/version`.
Use `127.0.0.1` rather than `localhost` — `localhost` can resolve to IPv6,
which Chrome's debug port does not listen on.

## 2. Record

```
python playwrightscriptrecord.py
```

It asks for a script filename, whether replays should log their output to
`<scriptname>.log` (default yes — emits `psl.logging(True)` into the
script), and the debug URL (`http://127.0.0.1:9222` is the best answer —
the `ws://` form also works but changes every Chrome restart). If several
tabs are open, it also asks which one the script should drive. Then a menu loops: Click, Double Click, Send Keys, Screen
Test, Wait, End. Every action is performed live in the browser as it is
recorded, and the script file is saved after each step.

**Immediate mode**: press Enter at the filename prompt instead of naming a
file. Each action still runs in the browser right away, but its code block
is printed to the console instead of being saved — great for experimenting
or driving the browser ad hoc. A copy-pasteable script header is printed at
the start, so you can turn the session into a real script by copying the
blocks into a `.py` file. Captures still save PNGs (named
`capture-immediate-...`).

- **Click / Double Click** — a screenshot pops up; click the target spot.
- **Send Keys** — type the text; write `\r` where Enter should be pressed.
  Before typing, `Home` + `Shift+End` are pressed so the field's existing
  content is selected and **replaced** (deterministic in every Windows edit
  control, unlike double-click selection which skips a leading `-`); the
  prefix repeats after each `\r`, and `psl.sendkeys(text,
  HomeShiftEndPrefix=False)` appends instead.
- **Screen Test** — name it, then drag a rectangle on the screenshot. The
  area as captured during recording is saved next to the script as
  `capture-<script>-<name>-<x1>,<y1>,<x2>,<y2>.png` (reusing a name
  replaces its capture), and the script gets a check at this point of the
  replay: re-grab the same area and raise the alarm if it no longer
  matches the saved PNG. The `matchLevel` (default 0.98; lower = more
  tolerant) and the alarm message are prompted with sensible defaults.
- **Wait** — inserts a pause; useful before screen tests when the page or a
  remote-desktop stream needs time to settle.

## 3. Replay

```
python myscript.py [debug-url]
```

Keep recorded scripts in this folder (they `import playwrightscriptlib`).
The optional argument overrides the debug URL recorded in the script.

If logging was enabled at record time (`psl.logging(True)` in the script),
every printed line — steps, alarms, operator answers, error tracebacks —
is also appended to `<scriptname>.log` next to the script; each run starts
with a dated header, so the log accumulates a history across runs.

To single-step a script, add `psl.pauseOnInfo(True)` (right after the
header, or just before a section you want to debug): every info line then
pauses with a popup offering **Next Step**, **Run Continuously** (turns
stepping off), and **STOP** (exit code 2). Closing the popup continues
without stepping; with no display it falls back to a console prompt where
plain Enter means next step.

Waits longer than 3 seconds show a countdown window with **Skip wait** and
**Abort script** (exit code 2) buttons — handy during long process waits.
Closing the window only hides the countdown; the remaining time still
elapses. The threshold is `WAIT_POPUP_MIN` at the top of the library.

`psl.clicksSettleTime(0.5)` adds a silent pause (fractional seconds fine)
after every click and double click — useful when the target UI needs a
beat to react between actions. `0` (the default) turns it off.

`psl.screenshotOnInfo(True)` saves a full screenshot on every info line —
a visual flight recorder for unattended runs. PNGs go to
`shots/<yymmdd hhmmss>/` next to the script — stamped with the time the
call was made, so every run gets its own folder (override with
`psl.screenshotOnInfo(True, folder=...)` for a fixed location). Files are
named `yymmdd hhmmss-<info message>.png`. Info lines before the browser
is connected are skipped; mind the disk on long runs at big viewports.

While running, the script prints a timestamped line before every step,
including your recorded comment:

```
[12:15:44] Click at (200, 60)
[12:15:44] Screen test 'boxframe' (matchLevel 0.98) -- check box unchanged
```

When a screen test fails, the script pops an always-on-top red window,
beeps loudly, and offers four choices:

- **Try compare again** — e.g. after you manually put the target page back
  into the right state
- **Show differences** — opens the saved capture and the current screen side
  by side, with the areas the comparator considers different tinted red
  (toggleable), then returns to the alarm. A further button, **Show current
  capture inside full frame grab**, displays the whole viewport as the
  script sees it with a blinking red box around the compare region — handy
  when the grabbed region isn't where (or what) you thought, e.g. the
  script is driving a different tab or the page is scrolled. With no
  display available it saves `compare-diff-*.png` files instead (including
  the boxed full-frame context) and prints their paths.
- **Skip and continue** — accept the mismatch and resume the script
- **Stop the script** — abort immediately (exit code 2)

A viewport-size mismatch at startup offers **Fix window size** — which
resizes the browser through the debug connection until the viewport
matches the recording exactly (Chrome updates and nudged windows shift it
by a few pixels) — plus **Check again**, **Continue anyway**, and
**Stop the script**. To pin the size unconditionally instead of asking,
replace the script's `psl.checkViewport(w, h)` line with
`psl.setViewport(w, h)`. Unexpected errors show a plain **Acknowledge**
alarm and exit. Closing an alarm window counts as its last (safest)
button.

## Notes

- Coordinates are CSS pixels from the top-left of the page viewport, so the
  browser window size (and zoom) must match record time. Scripts check this
  at startup and alarm on mismatch.
- Everything typed via Send Keys — including passwords — is stored as plain
  text in the generated script.
- One tab per script. The script finds its tab by a URL hint recorded at
  record time (the site's hostname).
- Keep the `capture-*.png` files next to the script when you move or deploy
  it — compares load them from the script's folder at run time.
- Comparison tuning knobs live at the top of `playwrightscriptlib.py`
  (`BLUR_RADIUS`, `DOWNSAMPLE`, `DIFF_TOLERANCE`).
