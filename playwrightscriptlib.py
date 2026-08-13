r"""playwrightscriptlib -- tiny helper library for recorded browser scripts.

Scripts talk to an already-running Chrome through its remote debug port
(launch Chrome with --remote-debugging-port=9222 and, if Chrome was already
running without the flag, a separate --user-data-dir).

Typical generated script:

    import sys
    import playwrightscriptlib as psl

    psl.alarmOnError()
    psl.connect(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9222",
                page_hint="example.com")
    psl.checkViewport(1280, 720)

    psl.click(512, 288)
    psl.sendkeys("hello\rworld")            # \r presses Enter
    psl.wait(2)
    # compare the live region against a capture PNG saved by the recorder;
    # on mismatch a loud alarm offers: try again / skip / stop the script
    psl.verifyFrame("capture-myscript-region-100,200,400,300.png",
                    (100, 200, 400, 300), 0.98, "Screen changed unexpectedly")

All coordinates are CSS pixels relative to the top-left corner of the page
viewport -- the same space frameGrab()/viewportGrab() screenshots are in.
"""

import io
import os
import sys
import threading
import time
import traceback

from PIL import Image, ImageChops, ImageDraw, ImageFilter
from playwright.sync_api import sync_playwright

# Tunables for frameSimilarity()/compareFrames(). Defaults are chosen so that
# lossy-stream compression noise (e.g. a Chrome Remote Desktop session canvas,
# JPEG artifacts) does not register, while real content changes still do.
BLUR_RADIUS = 1.5      # gaussian blur applied to both frames before diffing
DOWNSAMPLE = 2         # integer shrink factor applied after the blur
DIFF_TOLERANCE = 25    # per-pixel max channel difference (0-255) counted as "same"

WAIT_POPUP_MIN = 3     # wait() longer than this (seconds) shows the countdown popup

_pw = None
_browser = None
_page = None
_log = None
_pause_on_info = False
_clicks_settle_time = 0.0


def _log_write(text):
    if _log is not None:
        try:
            _log.write(text + "\n")
            _log.flush()
        except Exception:
            pass


def _emit(text):
    """Print a line and mirror it into the log file when logging is on."""
    print(text, flush=True)
    _log_write(text)


def _require_page():
    if _page is None:
        raise RuntimeError("Not connected -- call connect() first.")
    return _page


def _require_browser():
    if _browser is None:
        raise RuntimeError("Not connected -- call connect() first.")
    return _browser


def connect(url, page_hint=None):
    """Connect to a running Chrome's remote debug port and pick a tab.

    url is either the stable "http://host:port" form or the
    webSocketDebuggerUrl ("ws://.../devtools/browser/<guid>") shown at
    http://host:port/json/version.  Prefer the http form: the ws GUID
    changes every time Chrome restarts.

    page_hint: optional substring matched against tab URLs; the first tab
    whose URL contains it is driven, and it is an error when none does
    (better than silently driving the wrong tab).  Without a hint the
    first tab is used.

    Returns the underlying Playwright page (rarely needed).
    """
    global _pw, _browser, _page
    _pw = sync_playwright().start()
    try:
        _browser = _pw.chromium.connect_over_cdp(url)
    except Exception:
        _pw.stop()
        _pw = None
        raise
    if not _browser.contexts:
        raise RuntimeError("Connected, but the browser has no browser contexts.")
    pages = _browser.contexts[0].pages
    if not pages:
        raise RuntimeError("Connected, but the browser has no open tabs.")
    if page_hint:
        for p in pages:
            if page_hint in p.url:
                _page = p
                break
        else:
            raise RuntimeError(
                "No open tab URL contains %r. Open tabs: %s"
                % (page_hint, ", ".join(p.url for p in pages)))
    else:
        _page = pages[0]
    return _page


def disconnect():
    """Drop the connection (the browser itself keeps running)."""
    global _pw, _browser, _page
    try:
        if _browser is not None:
            _browser.close()
    finally:
        if _pw is not None:
            _pw.stop()
        _pw = _browser = _page = None


def listPages():
    """[(index, title, url), ...] for the open tabs of the connected browser."""
    out = []
    for i, p in enumerate(_require_browser().contexts[0].pages):
        try:
            title = p.title()
        except Exception:
            title = "<no title>"
        out.append((i, title, p.url))
    return out


def usePage(index):
    """Drive a different tab (index as shown by listPages())."""
    global _page
    _page = _require_browser().contexts[0].pages[index]
    return _page


def clicksSettleTime(seconds):
    """Silently pause this many seconds after every click()/doubleClick().

    Gives a slow UI (or a remote-desktop stream) time to react before the
    script moves on.  Fractional values are fine; 0 (the default) disables
    the pause.  The pause is a plain sleep -- no popup, no way to skip.
    """
    global _clicks_settle_time
    _clicks_settle_time = max(0.0, float(seconds))
    _emit("[%s] Click settle time set to %gs"
          % (time.strftime("%H:%M:%S"), _clicks_settle_time))


def click(x, y):
    """Single left click at (x, y), CSS pixels from the viewport's top-left."""
    _require_page().mouse.click(x, y)
    if _clicks_settle_time > 0:
        time.sleep(_clicks_settle_time)


def doubleClick(x, y):
    """Double left click at (x, y), CSS pixels from the viewport's top-left."""
    _require_page().mouse.dblclick(x, y)
    if _clicks_settle_time > 0:
        time.sleep(_clicks_settle_time)


def sendkeys(text, delay=20, HomeShiftEndPrefix=True):
    r"""Type text into the focused element; \r (or \n) presses Enter.

    delay is milliseconds between keystrokes -- keep a little so keys
    forwarded through remote-desktop sessions are not dropped.

    HomeShiftEndPrefix (default True) presses Home then Shift+End before
    typing, selecting the field's entire existing content so the typed
    text REPLACES it.  This is deterministic in every Windows edit control
    (unlike double-click word selection, which skips things like a leading
    '--').  The prefix repeats for each \r-separated chunk, so chained
    "value\rvalue" entries clean each field they land in; pass
    HomeShiftEndPrefix=False to append to existing content instead.
    """
    page = _require_page()
    normalized = text.replace("\r\n", "\r").replace("\n", "\r")
    parts = normalized.split("\r")
    for i, part in enumerate(parts):
        if part and HomeShiftEndPrefix:
            page.keyboard.press("Home")
            if delay:
                time.sleep(delay / 1000.0)
            page.keyboard.press("Shift+End")
            if delay:
                time.sleep(delay / 1000.0)
        if part:
            page.keyboard.type(part, delay=delay)
        if i < len(parts) - 1:
            page.keyboard.press("Enter")


def viewportGrab():
    """Screenshot of the whole visible viewport as a Pillow RGB image."""
    png = _require_page().screenshot(scale="css")
    return Image.open(io.BytesIO(png)).convert("RGB")


def viewportSize():
    """(width, height) of the page viewport in CSS pixels."""
    page = _require_page()
    return (int(page.evaluate("window.innerWidth")),
            int(page.evaluate("window.innerHeight")))


def _frame_and_full(x1, y1, x2, y2):
    """(cropped region, full viewport) taken from ONE screenshot, so the
    crop is guaranteed to be a piece of the returned full frame."""
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        raise ValueError(
            "frameGrab needs 0 <= x1 < x2 and 0 <= y1 < y2, got (%s, %s, %s, %s)"
            % (x1, y1, x2, y2))
    shot = viewportGrab()
    if x2 > shot.width or y2 > shot.height:
        raise ValueError(
            "frame (%s, %s, %s, %s) reaches outside the %sx%s viewport"
            % (x1, y1, x2, y2, shot.width, shot.height))
    return shot.crop((int(x1), int(y1), int(x2), int(y2))), shot


def frameGrab(x1, y1, x2, y2):
    """Screenshot of the viewport rectangle (x1, y1)-(x2, y2) as a Pillow image."""
    return _frame_and_full(x1, y1, x2, y2)[0]


def loadFrame(path):
    """Load a capture PNG (saved by the recorder) as a Pillow image.

    A relative path is tried as given first, then next to the running
    script, so replays find their captures no matter which directory they
    are started from.
    """
    candidates = [path]
    if not os.path.isabs(path) and sys.argv and sys.argv[0]:
        script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        candidates.append(os.path.join(script_dir, path))
    for candidate in candidates:
        if os.path.exists(candidate):
            return Image.open(candidate).convert("RGB")
    raise FileNotFoundError("Capture image not found: tried %s"
                            % " and ".join(candidates))


def _diff_map(img1, img2):
    """Per-pixel worst-channel difference (L mode) after the noise-suppressing
    blur + downsample pipeline shared by scoring and the diff viewer."""
    a = img1.convert("RGB")
    b = img2.convert("RGB")
    if b.size != a.size:
        b = b.resize(a.size, Image.LANCZOS)
    if BLUR_RADIUS > 0:
        blur = ImageFilter.GaussianBlur(BLUR_RADIUS)
        a = a.filter(blur)
        b = b.filter(blur)
    if DOWNSAMPLE > 1:
        size = (max(1, a.width // DOWNSAMPLE), max(1, a.height // DOWNSAMPLE))
        a = a.resize(size, Image.BILINEAR)
        b = b.resize(size, Image.BILINEAR)
    diff = ImageChops.difference(a, b)
    r, g, bl = diff.split()
    return ImageChops.lighter(ImageChops.lighter(r, g), bl)


def frameSimilarity(img1, img2):
    """Similarity of two frames: 0.0 (completely different) to 1.0 (identical).

    Both frames are blurred and downsampled first so lossy compression noise
    does not register; the score is the fraction of pixels whose channels
    then still differ by no more than DIFF_TOLERANCE.
    """
    if not isinstance(img1, Image.Image) or not isinstance(img2, Image.Image):
        raise TypeError("frameSimilarity expects two Pillow images")
    worst = _diff_map(img1, img2)
    hist = worst.histogram()
    within = sum(hist[:DIFF_TOLERANCE + 1])
    return within / float(worst.width * worst.height)


def _diff_mask(img1, img2):
    """Full-size mask (L mode, 0/255) of the pixels the comparator counts as
    different -- exactly the pixels that fail DIFF_TOLERANCE."""
    worst = _diff_map(img1, img2)
    mask = worst.point(lambda v: 255 if v > DIFF_TOLERANCE else 0)
    return mask.resize(img1.size, Image.NEAREST)


def _highlight(img, mask):
    """img with the masked (differing) pixels tinted red."""
    base = img.convert("RGB")
    red = Image.new("RGB", base.size, (255, 0, 48))
    return Image.composite(Image.blend(base, red, 0.55), base, mask)


def compareFrames(img1, img2, matchLevel):
    """True when the two frames match at the given strictness.

    matchLevel (0.0-1.0) is the minimum frameSimilarity() score that counts
    as a match.  0.98 is a good default; lower it if a noisy stream causes
    false alarms, raise it to catch smaller changes.
    """
    if not 0.0 <= matchLevel <= 1.0:
        raise ValueError("matchLevel must be between 0.0 and 1.0")
    return frameSimilarity(img1, img2) >= matchLevel


def _diff_viewer(expected, actual, score, matchLevel, box=None, full=None):
    """Side-by-side window: saved capture vs current screen, with a toggle
    that tints the differing areas red.  When box and full are given, a
    button shows the capture region inside the full frame grab, marked with
    a blinking red rectangle.  Blocks until closed."""
    import tkinter as tk
    from PIL import ImageTk

    expected = expected.convert("RGB")
    actual = actual.convert("RGB")
    if actual.size != expected.size:
        actual = actual.resize(expected.size, Image.LANCZOS)
    mask = _diff_mask(expected, actual)
    diff_pct = 100.0 * mask.histogram()[255] / float(mask.width * mask.height)

    root = tk.Tk()
    root.title("Compare differences")
    root.attributes("-topmost", True)

    scale = min(1.0, 0.44 * root.winfo_screenwidth() / expected.width,
                0.72 * root.winfo_screenheight() / expected.height)

    def fit(im):
        if scale >= 1.0:
            return im
        return im.resize((max(1, int(im.width * scale)),
                          max(1, int(im.height * scale))), Image.LANCZOS)

    photos = {
        False: (ImageTk.PhotoImage(fit(expected)), ImageTk.PhotoImage(fit(actual))),
        True: (ImageTk.PhotoImage(fit(_highlight(expected, mask))),
               ImageTk.PhotoImage(fit(_highlight(actual, mask)))),
    }

    tk.Label(root, text="Similarity %.4f (needs at least %s) -- %.1f%% of the area differs"
             % (score, matchLevel, diff_pct),
             font=("Segoe UI", 12, "bold")).pack(pady=(10, 4))
    row = tk.Frame(root)
    row.pack(padx=10, pady=4)
    panes = []
    for column, caption in ((0, "Expected (saved capture)"), (1, "Actual (screen now)")):
        pane = tk.Frame(row)
        pane.grid(row=0, column=column, padx=8)
        tk.Label(pane, text=caption, font=("Segoe UI", 11)).pack()
        img_label = tk.Label(pane)
        img_label.pack()
        panes.append(img_label)

    highlight_on = tk.BooleanVar(value=True)

    def refresh():
        left, right = photos[highlight_on.get()]
        panes[0].config(image=left)
        panes[1].config(image=right)

    context = {"win": None}

    def open_context():
        try:
            if context["win"] is not None and context["win"].winfo_exists():
                context["win"].lift()
                context["win"].focus_force()
                return
        except Exception:
            pass
        top = tk.Toplevel(root)
        context["win"] = top
        top.title("Compare region inside the full frame grab")
        top.attributes("-topmost", True)
        cscale = min(1.0, 0.88 * top.winfo_screenwidth() / full.width,
                     0.78 * top.winfo_screenheight() / full.height)
        if cscale < 1.0:
            disp = full.resize((max(1, int(full.width * cscale)),
                                max(1, int(full.height * cscale))), Image.LANCZOS)
        else:
            disp = full
        top._photo = ImageTk.PhotoImage(disp, master=top)
        tk.Label(top, text="Blinking red box = the region this compare grabs: "
                           "(%d, %d)-(%d, %d) of the %dx%d viewport"
                 % (box[0], box[1], box[2], box[3], full.width, full.height),
                 font=("Segoe UI", 11)).pack(pady=(8, 4))
        canvas = tk.Canvas(top, width=disp.width, height=disp.height,
                           highlightthickness=0)
        canvas.pack(padx=8, pady=4)
        canvas.create_image(0, 0, image=top._photo, anchor="nw")
        rx1, ry1, rx2, ry2 = [int(round(v * cscale)) for v in box]
        rect = canvas.create_rectangle(rx1, ry1, rx2, ry2,
                                       outline="#ff2020", width=3)
        blink = {"on": True}

        def blink_tick():
            try:
                if not canvas.winfo_exists():
                    return
                blink["on"] = not blink["on"]
                canvas.itemconfigure(
                    rect, outline="#ff2020" if blink["on"] else "#ffffff")
                top.after(400, blink_tick)
            except Exception:
                pass

        top.after(400, blink_tick)
        tk.Button(top, text="Close", font=("Segoe UI", 11, "bold"),
                  command=top.destroy, padx=16, pady=4).pack(pady=(4, 10))

    tk.Checkbutton(root, text="Highlight differences (red)", variable=highlight_on,
                   command=refresh, font=("Segoe UI", 11)).pack(pady=4)
    if full is not None and box is not None:
        tk.Button(root, text="Show current capture inside full frame grab",
                  font=("Segoe UI", 11), command=open_context,
                  padx=12, pady=4).pack(pady=4)
    tk.Button(root, text="Back to alarm", font=("Segoe UI", 12, "bold"),
              command=root.destroy, padx=20, pady=6).pack(pady=(4, 12))
    refresh()
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 4
    root.geometry("+%d+%d" % (x, y))
    root.lift()
    root.focus_force()
    root.mainloop()


def _save_diff_files(expected, actual, full=None, box=None):
    """Headless fallback for the diff viewer: write expected/actual/highlight
    (and, when available, full-frame context with the compare region boxed
    in red) PNGs to the current directory and return their absolute paths."""
    actual = actual.convert("RGB")
    if actual.size != expected.size:
        actual = actual.resize(expected.size, Image.LANCZOS)
    mask = _diff_mask(expected, actual)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    images = [("expected", expected), ("actual", actual),
              ("highlight", _highlight(actual, mask))]
    if full is not None and box is not None:
        context = full.convert("RGB")
        ImageDraw.Draw(context).rectangle(tuple(box), outline=(255, 32, 32), width=3)
        images.append(("context", context))
    paths = []
    for label, im in images:
        path = os.path.abspath("compare-diff-%s-%s.png" % (stamp, label))
        im.save(path)
        paths.append(path)
    return paths


def verifyFrame(baselinePath, box, matchLevel, message):
    """Compare the live screen region against a saved capture PNG.

    box is (x1, y1, x2, y2) -- the region the capture was taken from.  On a
    mismatch a loud alarm is raised offering the operator four choices:

      1. Try compare again -- e.g. after manually putting the target page
         back into the right state
      2. Show differences  -- side-by-side viewer of the saved capture and
         the current screen, with differing areas highlighted in red and a
         button that shows the capture region inside the full frame grab
         (blinking red box), then back to the alarm (saves diff PNGs
         instead when there is no display)
      3. Skip and continue -- accept the mismatch and resume the script
      4. Stop the script   -- abort immediately (exit code 2)

    Returns True when the frames matched (possibly after retries), False
    when the operator chose to skip.
    """
    if not 0.0 <= matchLevel <= 1.0:
        raise ValueError("matchLevel must be between 0.0 and 1.0")
    baseline = loadFrame(baselinePath)
    x1, y1, x2, y2 = box
    while True:
        fresh, full = _frame_and_full(x1, y1, x2, y2)
        score = frameSimilarity(baseline, fresh)
        if score >= matchLevel:
            return True
        detail = "%s  (similarity %.4f, needs at least %s)" % (message, score, matchLevel)
        region = (x1, y1, x2, y2)
        while True:
            choice = alarm(detail, buttons=("Try compare again", "Show differences",
                                            "Skip and continue", "Stop the script"))
            if choice != 1:
                break
            try:
                _diff_viewer(baseline, fresh, score, matchLevel, box=region, full=full)
            except Exception:
                try:
                    for path in _save_diff_files(baseline, fresh, full=full, box=region):
                        info("Saved %s" % path)
                except Exception as e:
                    info("Could not create diff images: %s" % e)
        if choice == 0:
            info("Retrying compare...")
            continue
        if choice == 2:
            info("Compare skipped by operator")
            return False
        info("Script stopped by operator")
        sys.exit(2)


def _fmt_remaining(seconds):
    whole = int(seconds) + (1 if seconds % 1 else 0)
    if whole >= 60:
        return "%d:%02d" % divmod(whole, 60)
    return "%ds" % whole


def _wait_window(seconds):
    """Countdown window for wait().  Returns (outcome, remaining_seconds)
    where outcome is 'done', 'skip', 'abort', or 'dismissed' (window
    closed; the caller still waits out the remaining time)."""
    import tkinter as tk

    deadline = time.monotonic() + seconds
    result = {"outcome": "dismissed"}
    root = tk.Tk()
    root.title("Waiting -- script paused on a timer")
    root.configure(bg="#1e5631")
    root.attributes("-topmost", True)
    tk.Label(root, text="⏳ WAITING", font=("Segoe UI", 22, "bold"),
             fg="white", bg="#1e5631").pack(padx=40, pady=(22, 4))
    remaining_label = tk.Label(root, text=_fmt_remaining(seconds),
                               font=("Segoe UI", 30, "bold"),
                               fg="white", bg="#1e5631")
    remaining_label.pack(padx=40, pady=2)
    tk.Label(root, text="of %g second(s)" % seconds, font=("Segoe UI", 12),
             fg="white", bg="#1e5631").pack(padx=40, pady=(0, 8))

    timer = {"id": None}

    def close(outcome):
        result["outcome"] = outcome
        if timer["id"] is not None:
            try:
                root.after_cancel(timer["id"])
            except Exception:
                pass
        root.destroy()

    row = tk.Frame(root, bg="#1e5631")
    row.pack(padx=30, pady=(4, 22))
    tk.Button(row, text="Skip wait", font=("Segoe UI", 12, "bold"),
              command=lambda: close("skip"), padx=16, pady=6).pack(side="left", padx=8)
    tk.Button(row, text="Abort script", font=("Segoe UI", 12, "bold"),
              command=lambda: close("abort"), padx=16, pady=6).pack(side="left", padx=8)

    def tick():
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timer["id"] = None
                close("done")
                return
            remaining_label.config(text=_fmt_remaining(remaining))
            timer["id"] = root.after(200, tick)
        except Exception:
            pass

    tick()
    root.protocol("WM_DELETE_WINDOW", lambda: close("dismissed"))  # X = hide, keep waiting
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry("+%d+%d" % (x, y))
    root.lift()  # deliberately no focus_force: informational, not an alarm
    root.mainloop()
    return result["outcome"], max(0.0, deadline - time.monotonic())


def wait(seconds):
    """Pause the script for the given number of seconds.

    Waits longer than WAIT_POPUP_MIN seconds show a topmost countdown
    window with two buttons: "Skip wait" continues the script right away,
    "Abort script" stops it (exit code 2).  Closing the window only
    dismisses the display -- the remaining time is still waited out.
    Shorter waits (and runs with no display) just sleep.
    """
    seconds = float(seconds)
    if seconds <= WAIT_POPUP_MIN:
        time.sleep(seconds)
        return
    try:
        outcome, remaining = _wait_window(seconds)
    except Exception:
        time.sleep(seconds)
        return
    if outcome == "abort":
        _emit("[%s] Script aborted by operator during wait" % time.strftime("%H:%M:%S"))
        sys.exit(2)
    if outcome == "skip":
        _emit("[%s] Wait skipped by operator (%s remaining)"
              % (time.strftime("%H:%M:%S"), _fmt_remaining(remaining)))
        return
    if outcome == "dismissed" and remaining > 0:
        _emit("[%s] Wait display dismissed -- continuing to wait %s"
              % (time.strftime("%H:%M:%S"), _fmt_remaining(remaining)))
        time.sleep(remaining)


def info(message):
    """Print a timestamped progress line; recorded scripts call this before
    each action so the console shows what is happening and when.

    With pauseOnInfo(True) active, each info line also pauses the script
    until the operator chooses how to proceed."""
    _emit("[%s] %s" % (time.strftime("%H:%M:%S"), message))
    if _pause_on_info:
        _step_pause(message)


def pauseOnInfo(enabled):
    """Single-step mode for recorded scripts.

    When enabled, every info() line pauses the script with a popup showing
    the message and three buttons:

      Next Step        -- run up to the next info() line, then pause again
      Run Continuously -- turn stepping off and let the script run normally
      STOP             -- abort the script immediately (exit code 2)

    The recorder writes an info() line before every action, so this steps
    through a script action by action.  Closing the popup (or console EOF
    when there is no display) counts as Run Continuously, so an accidental
    close never kills the script; the console fallback treats a plain
    Enter as Next Step.
    """
    global _pause_on_info
    _pause_on_info = bool(enabled)
    _emit("[%s] Step mode %s" % (time.strftime("%H:%M:%S"),
                                 "on -- pausing at every step" if enabled else "off"))


def _step_pause(message):
    global _pause_on_info
    buttons = ("Next Step", "Run Continuously", "STOP")
    try:
        choice = _pause_window(message, buttons)
    except Exception:
        choice = _pause_console(message, buttons)
    if choice == 0:
        return
    if choice == 1:
        _pause_on_info = False
        _emit("[%s] Running continuously -- step mode off" % time.strftime("%H:%M:%S"))
        return
    _emit("[%s] Script stopped by operator (step mode)" % time.strftime("%H:%M:%S"))
    sys.exit(2)


def _pause_window(message, buttons):
    import tkinter as tk

    result = {"choice": 1}  # closing the window = Run Continuously
    root = tk.Tk()
    root.title("Step mode -- script paused")
    root.configure(bg="#1f4e79")
    root.attributes("-topmost", True)
    tk.Label(root, text="⏸ PAUSED", font=("Segoe UI", 24, "bold"),
             fg="white", bg="#1f4e79").pack(padx=40, pady=(24, 8))
    tk.Label(root, text=message, font=("Segoe UI", 14), fg="white",
             bg="#1f4e79", wraplength=640, justify="center").pack(padx=40, pady=8)

    def pick(index):
        result["choice"] = index
        root.destroy()

    row = tk.Frame(root, bg="#1f4e79")
    row.pack(padx=30, pady=(8, 24))
    for i, label in enumerate(buttons):
        tk.Button(row, text=label, font=("Segoe UI", 12, "bold"),
                  command=lambda i=i: pick(i), padx=16, pady=6).pack(side="left", padx=8)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry("+%d+%d" % (x, y))
    root.lift()
    root.focus_force()
    root.mainloop()
    return result["choice"]


def _pause_console(message, buttons):
    for i, label in enumerate(buttons, 1):
        print("  %d. %s" % (i, label), flush=True)
    while True:
        try:
            s = input("*** Paused -- choose 1-%d [1]: " % len(buttons)).strip()
        except EOFError:
            return 1  # nobody at the console -> run continuously
        if not s:
            return 0  # plain Enter steps to the next info line
        if s.isdigit() and 1 <= int(s) <= len(buttons):
            return int(s) - 1


def logging(enabled, path=None):
    """Append every output line this library prints to a log file.

    psl.logging(True) opens <scriptname>.log next to the running script in
    append mode (each run adds a dated session header) and mirrors info()
    lines, alarm lines, operator answers and error tracebacks into it.
    psl.logging(False) turns it off.  path overrides the file location.
    """
    global _log
    if _log is not None:
        try:
            _log.close()
        except Exception:
            pass
        _log = None
    if not enabled:
        return
    if path is None:
        base = sys.argv[0] if sys.argv and sys.argv[0] else "playwrightscript"
        path = os.path.splitext(os.path.abspath(base))[0] + ".log"
    _log = open(path, "a", encoding="utf-8")
    _log.write("===== %s -- log opened by %s =====\n"
               % (time.strftime("%Y-%m-%d %H:%M:%S"),
                  os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "?"))
    _log.flush()
    info("Logging to %s" % os.path.abspath(path))


def _beep_loop(stop):
    try:
        import winsound
    except ImportError:
        winsound = None
    while not stop.is_set():
        if winsound is not None:
            for freq in (1000, 741):
                if stop.is_set():
                    return
                try:
                    winsound.Beep(freq, 350)
                except RuntimeError:
                    pass
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
        stop.wait(0.2)


def _alarm_window(message, buttons):
    import tkinter as tk

    result = {"choice": len(buttons) - 1}  # closing the window = last button
    root = tk.Tk()
    root.title("ALARM -- script needs attention")
    root.configure(bg="#b00020")
    root.attributes("-topmost", True)
    tk.Label(root, text="⚠ ALARM", font=("Segoe UI", 28, "bold"),
             fg="white", bg="#b00020").pack(padx=40, pady=(30, 10))
    tk.Label(root, text=message, font=("Segoe UI", 16), fg="white",
             bg="#b00020", wraplength=640, justify="center").pack(padx=40, pady=10)

    def pick(index):
        result["choice"] = index
        root.destroy()

    row = tk.Frame(root, bg="#b00020")
    row.pack(padx=30, pady=(10, 30))
    for i, label in enumerate(buttons):
        tk.Button(row, text=label, font=("Segoe UI", 13, "bold"),
                  command=lambda i=i: pick(i), padx=18, pady=8).pack(side="left", padx=8)
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry("+%d+%d" % (x, y))
    root.lift()
    root.focus_force()
    root.bell()
    root.mainloop()
    return result["choice"]


def _alarm_console(message, buttons):
    if len(buttons) == 1:
        try:
            input("*** Press Enter to acknowledge the alarm... ")
        except EOFError:
            pass
        return 0
    for i, label in enumerate(buttons, 1):
        print("  %d. %s" % (i, label), flush=True)
    while True:
        try:
            s = input("*** Choose 1-%d: " % len(buttons)).strip()
        except EOFError:
            return len(buttons) - 1  # nobody there to answer -> last button
        if s.isdigit() and 1 <= int(s) <= len(buttons):
            return int(s) - 1


def alarm(message, buttons=("Acknowledge",)):
    """Show message on this computer and beep loudly until an operator answers.

    Blocks until one of the buttons is clicked and returns its 0-based
    index.  With the default single button this simply waits for an
    acknowledge.  Falls back to a numbered console prompt when no display
    is available; closing the window (or console EOF) counts as the LAST
    button, so put the safe/abort choice last.
    """
    _emit("*** ALARM: %s" % message)
    stop = threading.Event()
    beeper = threading.Thread(target=_beep_loop, args=(stop,), daemon=True)
    beeper.start()
    try:
        try:
            choice = _alarm_window(message, tuple(buttons))
        except Exception:
            choice = _alarm_console(message, tuple(buttons))
    finally:
        stop.set()
        beeper.join(timeout=2)
    _emit("*** Alarm answered: %s" % buttons[choice])
    return choice


def alarmOnError():
    """Make any uncaught exception in the script raise alarm(), then exit 1."""
    def _hook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        _log_write("".join(traceback.format_exception(exc_type, exc, tb)).rstrip())
        summary = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        alarm("Script error: %s" % summary)
    sys.excepthook = _hook


def setViewport(width, height):
    """Resize the real browser window until the page viewport is exactly
    width x height CSS pixels.

    Works through the debug connection (no OS window fiddling), restoring
    a maximized window to normal first when necessary.  Raises
    RuntimeError when the size cannot be reached (screen too small,
    below the browser's minimum window size, zoom is not 100%, ...).

    Put psl.setViewport(w, h) at the start of a script to PIN the size it
    was recorded at: Chrome updates and nudged windows change the
    viewport by a few pixels, which shifts every recorded coordinate and
    rescales every capture.
    """
    width, height = int(width), int(height)
    page = _require_page()
    session = page.context.new_cdp_session(page)
    try:
        target_info = session.send("Browser.getWindowForTarget")
        window_id = target_info["windowId"]
        if target_info["bounds"].get("windowState", "normal") != "normal":
            session.send("Browser.setWindowBounds",
                         {"windowId": window_id, "bounds": {"windowState": "normal"}})
            time.sleep(0.3)
        for _ in range(6):
            vw, vh = viewportSize()
            if (vw, vh) == (width, height):
                break
            bounds = session.send("Browser.getWindowBounds",
                                  {"windowId": window_id})["bounds"]
            session.send("Browser.setWindowBounds",
                         {"windowId": window_id,
                          "bounds": {"width": bounds["width"] + (width - vw),
                                     "height": bounds["height"] + (height - vh)}})
            time.sleep(0.3)
        vw, vh = viewportSize()
        if (vw, vh) != (width, height):
            raise RuntimeError(
                "Could not reach viewport %dx%d (got %dx%d) -- is the screen "
                "large enough and zoom at 100%%?" % (width, height, vw, vh))
        _emit("[%s] Viewport set to %dx%d" % (time.strftime("%H:%M:%S"), width, height))
    finally:
        try:
            session.detach()
        except Exception:
            pass


def checkViewport(width, height):
    """Alarm unless the live viewport matches the recorded size.

    Recorded coordinates only line up when the browser window (and zoom)
    match record time, so a size mismatch would silently click the wrong
    places.  The alarm offers: Fix window size (resize the browser via
    setViewport to match the recording), Check again, Continue anyway, or
    Stop the script (exit code 2).
    """
    while True:
        w, h = viewportSize()
        if (w, h) == (width, height):
            return
        choice = alarm(
            "Viewport is %dx%d but this script was recorded at %dx%d. "
            "'Fix window size' resizes the browser to match (also check zoom)."
            % (w, h, width, height),
            buttons=("Fix window size", "Check again", "Continue anyway",
                     "Stop the script"))
        if choice == 0:
            try:
                setViewport(width, height)
            except Exception as e:
                _emit("[%s] Could not resize: %s" % (time.strftime("%H:%M:%S"), e))
            continue
        if choice == 1:
            info("Re-checking viewport...")
            continue
        if choice == 2:
            info("Viewport mismatch ignored by operator")
            return
        info("Script stopped by operator")
        sys.exit(2)
