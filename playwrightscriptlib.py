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

_pw = None
_browser = None
_page = None


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


def click(x, y):
    """Single left click at (x, y), CSS pixels from the viewport's top-left."""
    _require_page().mouse.click(x, y)


def doubleClick(x, y):
    """Double left click at (x, y), CSS pixels from the viewport's top-left."""
    _require_page().mouse.dblclick(x, y)


def sendkeys(text, delay=20):
    r"""Type text into the focused element; \r (or \n) presses Enter.

    delay is milliseconds between keystrokes -- keep a little so keys
    forwarded through remote-desktop sessions are not dropped.
    """
    page = _require_page()
    normalized = text.replace("\r\n", "\r").replace("\n", "\r")
    parts = normalized.split("\r")
    for i, part in enumerate(parts):
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


def wait(seconds):
    """Pause the script for the given number of seconds."""
    time.sleep(float(seconds))


def info(message):
    """Print a timestamped progress line; recorded scripts call this before
    each action so the console shows what is happening and when."""
    print("[%s] %s" % (time.strftime("%H:%M:%S"), message), flush=True)


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
    print("*** ALARM: %s" % message, flush=True)
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
    print("*** Alarm answered: %s" % buttons[choice], flush=True)
    return choice


def alarmOnError():
    """Make any uncaught exception in the script raise alarm(), then exit 1."""
    def _hook(exc_type, exc, tb):
        traceback.print_exception(exc_type, exc, tb)
        summary = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        alarm("Script error: %s" % summary)
    sys.excepthook = _hook


def checkViewport(width, height):
    """Alarm unless the live viewport matches the recorded size.

    Recorded coordinates only line up when the browser window (and zoom)
    match record time, so a size mismatch would silently click the wrong
    places.  The alarm lets the operator resize the window and check
    again, continue anyway, or stop the script (exit code 2).
    """
    while True:
        w, h = viewportSize()
        if (w, h) == (width, height):
            return
        choice = alarm(
            "Viewport is %dx%d but this script was recorded at %dx%d. "
            "Resize the browser window (and check zoom)." % (w, h, width, height),
            buttons=("Check again", "Continue anyway", "Stop the script"))
        if choice == 0:
            info("Re-checking viewport...")
            continue
        if choice == 1:
            info("Viewport mismatch ignored by operator")
            return
        info("Script stopped by operator")
        sys.exit(2)
