# Playwright Script Recorder

Record point-and-click browser scripts, then replay them through Chrome's
remote debug port. Works on regular websites and on the Chrome Remote
Desktop web client (the image comparison tolerates its video-compression
noise).

- `playwrightscriptlib.py` — small runtime library the recorded scripts use
- `playwrightscriptlayout.py` — named layout storage and shared screenshot pickers
- `playwrightscriptocr.py` — local single-line text recognition using Tesseract
- `playwrightscriptpopup.py` — popup image detection across the full viewport
- `playwrightscriptrecord.py` — interactive recorder that writes the scripts

## Named screen layouts

Keep the sequence in the Python script and the screen geometry in a layout
folder. Copy **`playwrightscriptlib.py`, `playwrightscriptlayout.py`,
`playwrightscriptocr.py`, and `playwrightscriptpopup.py`** beside scripts used in another project.

```python
import playwrightscriptlib as psl

psl.connect("http://127.0.0.1:9222", page_hint="remotedesktop.google.com")
psl.loadLayout("layouts/new-screen")
psl.checkViewport()
psl.click("alignment-tab")
psl.doubleClick("x-field")
psl.sendkeys("100")
psl.click("move-absolute")
psl.wait(5)
actual_pixels = psl.frameGrab("actual-xy")
psl.verifyFrame("focus-ready", matchLevel=0.99,
                message="Focus is not ready", delay=10, retrycount=24)
```

`psl.loadLayout()` opens a folder browser where the operator can choose or
create a folder. A supplied relative path is resolved against the current
working directory. Importing the library opens no dialogs. If a named action
is used before loading a layout, it opens the same folder browser.

Each name is stored as `<name>.json`. Points contain X/Y coordinates; regions
contain rectangle bounds; verification frames contain bounds and a relative
reference PNG filename. `layout.json` stores the format version and viewport
size, recorded on first use. Thresholds, waits, retry counts, messages, and
action order remain in the script. Names can include hyphens and underscores;
use different names for points, plain regions, and verification frames.

On first use of a missing name:

- **Click / double-click:** choose Define location, pick the target on a frozen
  screenshot, and the library saves the point before executing the requested
  action exactly once.
- **Capture region:** choose Define rectangle and drag its bounds on a frozen
  screenshot. `frameGrab(name)` returns fresh pixels; `layoutBounds(name)`
  returns the bounds without capturing a reference image.
- **Verification frame:** first wait for or establish the intended expected
  state. Choose Define reference, select the rectangle, and explicitly accept
  the crop preview as the expected reference. The normal live verification
  then runs with the script's threshold and retry options.

Canceling a definition or folder selection stops the script with exit code 2.
Definition dialogs are modal to an existing operator window; their own Stop,
close, and Escape controls cancel the operation. The browser remains usable
while waiting for the expected state.
Saving must succeed before a click is sent. Invalid metadata, missing/corrupt
reference PNGs, wrong image dimensions, and mismatched viewport sizes produce
errors; these are not treated as permission to capture a new reference.

Use a **new layout folder when the screen arrangement changes**. The viewport
check detects dimension changes; it cannot detect panels moving within the
same-size screen. Named actions never automatically rescale old coordinates.
At startup, `checkViewport()` offers **Restore saved size**, **Check again**,
or **Stop the script** if Chrome's page area differs from the selected layout.
Restoration adjusts the browser window and rechecks the actual dimensions;
the script continues only on an exact match. Failed restoration stays in the
dialog with the error displayed. Saved layout dimensions, points and images
remain unchanged. Closing the dialog stops the script. Viewport changes
during an action still raise an error before that pending input.
To explicitly replace one item in a selected layout:

```python
psl.definePoint("alignment-tab")   # Redefine only; does not click
psl.defineRegion("actual-xy")      # Returns rectangle bounds
psl.defineFrame("focus-ready")     # Confirms a new expected-state reference
```

Replacement definitions keep the previous files intact until the new
definition is accepted and saved. Previous reference PNGs are retained when
replaced; the entry JSON identifies the current one.

Legacy numeric/path calls remain supported without choosing a layout, including
`click(x, y)`, `doubleClick(x, y)`, `frameGrab(x1, y1, x2, y2)`, and
`verifyFrame(path, box, matchLevel, message, ...)`. Explicit
`checkViewport(width, height)` also keeps its previous behavior.

Run the offline layout, library, and recorder checks with
`python -B -m unittest discover -s tests -v`. These tests use simulated browser
actions and do not connect to a machine.

## Wait for a popup before mouse or keyboard input

For notices such as Chrome Remote Desktop's sharing banner, enable a guard
after connecting, loading a layout, and checking the viewport:

```python
psl.enableClickGuard("crd-popup", timeout=30)
psl.click("move-absolute")  # click, doubleClick, and sendkeys use this guard.
```

`crd-popup` is a normal frame entry: its JSON stores the reference PNG and the
rectangle used to select it. The guard searches the **whole viewport** for
that patch, so moving the popup does not evade the check. Select a tight,
distinctive patch of constant text or an icon; exclude account names and other
changing content. A missing entry uses the usual definition/confirmation UI.
References smaller than 20x10 pixels or with insufficient contrast fail setup.

To outline a reference from an existing full-viewport screenshot:

```python
psl.enableClickGuard("crd-popup", timeout=30,
                     referenceScreenshot=r"C:\captures\popup-visible.png")
# Or redefine a frame separately:
psl.defineFrame("crd-popup", screenshot=r"C:\captures\popup-visible.png")
```

The saved screenshot must match the selected layout's viewport dimensions.
Importing a screenshot explicitly redefines that frame after operator
confirmation. Later runs omit the screenshot argument to reuse the saved entry.

Immediately before each `click()` or `doubleClick()`, and before each keyboard
operation in `sendkeys()`, the guard takes fresh
screenshots until it sees two consecutive clear captures, 0.25 seconds apart.
Popup reappearance resets that count. The 30-second budget includes captures,
detection and polling; each screenshot has a timeout of at most two seconds.
The wait sends no input and has no Skip button. It raises `ClickGuardTimeout`
and saves the final screenshot under the run folder if time expires.
Screenshot, detection, or changed page/layout/viewport errors raise
`ClickGuardError` without sending the pending input. Already-issued input is
never retried. `sendkeys()` checks before Home, Shift+End, each text chunk,
and each Enter. Each has its own 30-second wait budget. This protects newline-only
calls and chained fields too. A text chunk is still one typing operation;
the popup is not checked between individual characters in that chunk. If a
later operation fails, earlier keys may already have been sent.

Optional `matchLevel=0.90` is the normalized template correlation threshold;
`pollInterval=0.25` controls the interval. OpenCV headless (which installs NumPy)
is required; it loads only when the guard is configured. Image scale must stay
the same, and a new banner design may require a new reference. The brief gap
between the final screenshot and input remains; keep downstream state
and position checks. Standalone screenshots are not guarded. The general
library defaults to guard disabled; `disableClickGuard()` explicitly disables
it for both mouse and keyboard input. The existing `enableClickGuard()` name
and setup call remain valid for all three input functions.

## Read a line of text with OCR

After connecting and loading a layout, capture and read a named rectangle:

```python
text = psl.grabOCRTextLine("actual-xy")
print(text)  # For example: X=100.3, Y=0 µm
```

The name uses the same plain region definition as `frameGrab(name)`. If it is
missing, the operator defines the rectangle using the normal layout dialog.
Select the entire line, allowing room for its longest expected contents. Each
call reads a fresh screenshot crop and returns a string with outer whitespace
removed, preserving internal spacing and the characters recognized by OCR.
No reference image is needed. Named regions retain the layout's viewport checks.

Install **Tesseract OCR with English language data** on the computer running
the script; see the [Tesseract installation documentation](https://tesseract-ocr.github.io/tessdoc/Installation.html).
The library uses the executable directly, so no additional Python package is
required. It checks PATH and standard Windows installation directories. An
explicit executable path can be supplied on the call:

```python
text = psl.grabOCRTextLine(
    "actual-xy", tesseract_cmd=r"C:\Program Files\Tesseract-OCR\tesseract.exe")
```

Four numeric CSS coordinates are also supported, as with `frameGrab`:
`psl.grabOCRTextLine(10, 20, 210, 45)`. Right and bottom bounds are exclusive.

The OCR executable and English data are checked before opening a region
definition dialog or capturing pixels. OCR runs locally in single-line mode;
each subprocess has a 15-second timeout. Missing OCR support, execution
failures, empty output, or unexpected multiple lines raise `psl.OCRError`.
Layout and screenshot errors propagate normally. Callers checking machine
coordinates should parse the returned string and compare the values against
their commanded positions and tolerance.

## Setup

Python 3.9 or newer with tkinter (included in the standard Windows
installer) — but **not 3.14.0–3.14.6**; see the Python version note at the
bottom. Then:

```
pip install -r requirements.txt
```

(Playwright, Pillow, and OpenCV headless for popup matching. No
`playwright install` is needed, since we attach to your existing Chrome.)

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
`runs\<datetime>\<scriptname>.log` (default yes — emits `psl.logging(True)`
into the script), and the debug URL (`http://127.0.0.1:9222` is the best answer —
the `ws://` form also works but changes every Chrome restart). If several
tabs are open, it also asks which one the script should drive. It then offers
named layouts (default yes) and a layout folder picker. Answer `n` to keep
recording literal coordinates and capture paths. Then a menu loops: Click, Double Click, Send Keys, Screen
Test, Wait, End. Every action is performed live in the browser as it is
recorded, and the script file is saved after each step.

**Immediate mode**: press Enter at the filename prompt instead of naming a
file. Each action still runs in the browser right away, but its code block
is printed to the console instead of being saved — great for experimenting
or driving the browser ad hoc. A copy-pasteable script header is printed at
the start, so you can turn the session into a real script by copying the
blocks into a `.py` file. Named definitions and their reference PNGs are saved
in the selected layout. Legacy captures still save `capture-immediate-...` PNGs.

- **Click / Double Click** — in named mode, give the point a name, then define
  it on a screenshot. The generated call uses that name. Legacy mode records
  the picked coordinates directly.
- **Send Keys** — type the text; write `\r` where Enter should be pressed.
  Before typing, `Home` + `Shift+End` are pressed so the field's existing
  content is selected and **replaced** (deterministic in every Windows edit
  control, unlike double-click selection which skips a leading `-`); the
  prefix repeats after each `\r`, and `psl.sendkeys(text,
  HomeShiftEndPrefix=False)` appends instead.
- **Screen Test** — name it, then drag a rectangle on the screenshot. Named
  mode requires accepting the expected-state preview, stores it in the layout,
  and emits `verifyFrame(name, matchLevel=..., ...)`. In legacy mode the
  area as captured during recording is saved next to the script as
  `capture-<script>-<name>-<x1>,<y1>,<x2>,<y2>.png` (reusing a name
  replaces its capture), and the script gets a check at this point of the
  replay: re-grab the same area and raise the alarm if it no longer
  matches the saved PNG. The `matchLevel` (default 0.98; lower = more
  tolerant) and the alarm message are prompted with sensible defaults.
  The recorder also asks for a per-try `delay` (seconds, default 0) and a
  number of `tries` (default 1); anything beyond the defaults is emitted
  as `delay=` / `retrycount=` on the generated `psl.verifyFrame(...)` —
  e.g. `delay=10, retrycount=90` turns a screen test into a poll that
  succeeds the moment the screen matches, often nicer than a long fixed
  `wait()` in front of it. Whenever a delay or retries are in play, a
  quiet countdown window shows `Verifying frame / <name> / Try n of m in
  Xs`, a live `(N seconds until timeout)` estimate, and the last
  similarity score, with **Try now** / **Abort verify** (returns False,
  script continues) / **Abort script** (exit code 2) buttons; closing it
  hides the window and the cycle continues silently. When a mismatch
  should not stop anyone, add `fallthru=True` to the generated
  `psl.verifyFrame(...)` call by hand (the recorder never emits it): the
  test then just logs the miss and returns `False` for your own code to
  act on — e.g. `if not psl.verifyFrame(...): ...` to branch on which of
  two screens came up — instead of raising the alarm.
- **Wait** — inserts a pause; useful before screen tests when the page or a
  remote-desktop stream needs time to settle.

## 3. Replay

```
python myscript.py [debug-url] [layout-folder]
```

Keep recorded scripts in this folder (they `import playwrightscriptlib`).
The first optional argument overrides the debug URL recorded in the script.
Named recordings accept a second argument for the layout folder; when omitted,
replay opens the folder picker. Legacy recordings only use the debug URL.

If logging was enabled at record time (`psl.logging(True)` in the script),
every printed line — steps, alarms, operator answers, error tracebacks —
is also written to `runs/<yymmdd hhmmss>/<scriptname>.log`: the dated run
folder is created next to the script when logging (or info screenshots)
first switches on, and both features share it, so everything from one run
lands in one place. Pass `psl.logging(True, path=...)` for a fixed file
instead (appended across runs with dated session headers).

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
a visual flight recorder for unattended runs. PNGs go into the same
per-run `runs/<yymmdd hhmmss>/` folder as the log (override with
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
- **Show differences** — opens a live alignment view with the saved reference
  beside the latest screen capture. The capture, similarity score, and red
  difference highlights refresh about once per second. Use **Pause live
  refresh** to inspect a frame, **Refresh now** for one new capture, and
  **Resume live refresh** to restart automatic updates. The reference image
  and capture rectangle stay fixed while you manually reposition or resize
  the application window. A **View pixels** button opens a
  zoomable side-by-side pixel inspector (nearest-neighbour blocks with grid
  lines, synced scrolling): click a pixel on either side to highlight the
  same spot on both and see the two RGB values with their max channel
  difference. It follows live captures while preserving zoom, scroll, and
  pixel selection. Zoomed images are bounded to limit memory use. A further
  button, **Show current
  capture inside full frame grab**, displays the whole viewport as the
  script sees it with a blinking red box around the compare region — handy
  when the grabbed region isn't where (or what) you thought, e.g. the
  script is driving a different tab or the page is scrolled. That view also
  follows the same live captures. When aligned, choose **Back to alarm**, then
  **Try compare again** for a new normal verification. A matching preview
  keeps the script paused. With no
  display available it saves `compare-diff-*.png` files instead (including
  the boxed full-frame context) and prints their paths.
- **Skip and continue** — accept the mismatch and resume the script
- **Stop the script** — abort immediately (exit code 2)

Live capture requests time out after two seconds. On capture failure, the
viewer marks the retained image as stale and clears the current match status;
it keeps checking while live refresh is enabled. Named layouts also require
the captured viewport to match the saved dimensions, so restore those
dimensions if the viewer reports a viewport change. A viewport mismatch
detected before verification still follows the existing viewport checks.
The live viewer allows normal interaction with the browser. Closing it with
Back, Escape, or the window's X returns to the alarm and stops its refreshes.

A viewport-size mismatch in the legacy `checkViewport(w, h)` call offers **Fix window size** — which
resizes the browser through the debug connection until the viewport
matches the recording exactly (Chrome updates and nudged windows shift it
by a few pixels) — plus **Check again**, **Continue anyway**, and
**Stop the script**. To pin the size unconditionally instead of asking,
replace the script's `psl.checkViewport(w, h)` line with
`psl.setViewport(w, h)`. Unexpected errors show a plain **Acknowledge**
alarm and exit. The named-layout `checkViewport()` call instead offers the
restoration flow described above, with no **Continue anyway** option.
Closing an alarm window counts as its last (safest)
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

## Python version

**Minimum: Python 3.9. If you use the 3.14 line, use 3.14.7 or newer.**

- 3.9 is the floor because the Playwright package requires it (the code
  itself uses nothing newer). Any 3.9–3.13 release is fine.
- 3.14.0 through 3.14.6 must be avoided for anything but a quick test:
  they have an asyncio bug ([CPython #152569](https://github.com/python/cpython/issues/152569),
  fixed in 3.14.7, released 2026-08-05) in which `asyncio.wait()` never
  forgets a finished task when one of the futures it raced never resolves.
  Playwright's sync API races every browser call against exactly such a
  future and attaches a stack snapshot to the task, so on those releases
  every click, keystroke and screenshot pins its stack — including the
  full-viewport screenshots behind `verifyFrame()` and `screenshotOnInfo()`
  — for the life of the connection. A long replay grows by roughly one
  screenshot per action (3.4 GB was observed on a laser-writer job); on
  3.14.7 the same run stays flat.
