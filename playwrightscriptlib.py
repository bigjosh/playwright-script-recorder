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
    psl.loadLayout("layouts/desktop")     # omit path for a folder picker
    psl.checkViewport()

    psl.click("message-field")
    psl.sendkeys("hello\rworld")            # \r presses Enter
    psl.wait(2)
    # compare the live region against the named layout's reference;
    # on mismatch a loud alarm offers: try again / skip / stop the script
    psl.verifyFrame("message-ready", matchLevel=0.98,
                    message="Screen changed unexpectedly")

All coordinates are CSS pixels relative to the top-left corner of the page
viewport -- the same space frameGrab()/viewportGrab() screenshots are in.
Named definitions are stored by playwrightscriptlayout.py. Legacy numeric
coordinates and explicit capture paths remain supported without a layout.
"""

import io
import math
import os
import re
import sys
import threading
import time
import traceback

from PIL import Image, ImageChops, ImageDraw, ImageFilter
from playwright.sync_api import sync_playwright

import playwrightscriptlayout as _layouts
import playwrightscriptocr as _ocr
import playwrightscriptpopup as _popup

# Public error type for callers that want to handle an unreadable OCR region.
OCRError = _ocr.OCRError

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
_shot_dir = None
_shot_warned = False
_run_dir = None
_layout = None
_click_guard = None


class ClickGuardError(RuntimeError):
    """The screen could not be confirmed clear before requested input."""


class ClickGuardTimeout(ClickGuardError):
    """A popup prevented confirming a clear screen within the time limit."""


def _run_folder():
    """runs\\<yymmdd hhmmss>\\ next to the running script.

    Created on first use and then shared for the rest of the run, so the
    log file and the info screenshots of one run land in one folder.
    """
    global _run_dir
    if _run_dir is None:
        base = sys.argv[0] if sys.argv and sys.argv[0] else "."
        _run_dir = os.path.join(os.path.dirname(os.path.abspath(base)),
                                "runs", time.strftime("%y%m%d %H%M%S"))
        os.makedirs(_run_dir, exist_ok=True)
    return _run_dir


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


def loadLayout(path=None):
    """Select a named layout folder; omit path to open the folder picker.

    Importing this module never opens a dialog. A new folder records its
    viewport on the first layout operation, after a browser is connected.
    Names are resolved only inside this folder, never against the working
    directory or another layout. Returns the selected Layout object.
    """
    global _layout
    if path is None:
        _emit("Opening screen layout folder picker; waiting for operator selection")
        path = _layouts.choose_layout_folder()
    selected = _layouts.Layout(path)
    _layout = selected
    _emit("Layout selected: %s" % selected.path)
    return selected


def _require_layout():
    if _layout is None:
        loadLayout()
    return _layout


def layoutViewport():
    """Return the layout's viewport size, refusing mismatched browser geometry."""
    selected = _require_layout()
    selected.check_viewport(viewportSize())
    return selected.viewport


def _layout_entry(name, kind, redefine=False):
    selected = _require_layout()
    selected.check_viewport(viewportSize())
    resolver = selected.define if redefine else selected.resolve
    entry = resolver(name, kind, viewportGrab)
    # The operator may have resized the browser while defining this action.
    selected.check_viewport(viewportSize())
    return entry


def definePoint(name):
    """Define/redefine a named point on a screenshot, without clicking it."""
    entry = _layout_entry(name, "point", redefine=True)
    return (entry["x"], entry["y"])


def defineRegion(name):
    """Define/redefine a named capture rectangle, without a reference image."""
    return tuple(_layout_entry(name, "region", redefine=True)["box"])


def defineFrame(name, *, screenshot=None):
    """Confirm and save a reference; optionally pick from a saved screenshot.

    A saved full-viewport image must have this layout's viewport dimensions.
    Selection and confirmation use the same recording UI, without any clicks
    sent to the browser. Returns (PNG path, box).
    """
    if screenshot is None:
        entry = _layout_entry(name, "frame", redefine=True)
    else:
        selected = _require_layout()
        selected.check_viewport(viewportSize())
        saved = loadFrame(os.fspath(screenshot))
        selected.check_viewport(saved.size)
        entry = selected.define(name, "frame", lambda: saved.copy())
        selected.check_viewport(viewportSize())
    return entry["image"], tuple(entry["box"])


def layoutBounds(name):
    """Return a named region's bounds, asking to define it when missing."""
    return tuple(_layout_entry(name, "region")["box"])


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
    _emit("Starting Playwright driver")
    _pw = sync_playwright().start()
    _emit("Playwright driver started")
    try:
        _emit("Connecting to browser debug endpoint")
        _browser = _pw.chromium.connect_over_cdp(url)
        _emit("Browser debug connection established")
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
    _emit("Browser tab selected")
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


def enableClickGuard(name, *, timeout=30.0, matchLevel=0.90, pollInterval=0.25,
                     referenceScreenshot=None):
    """Guard click/doubleClick and each keyboard operation in sendkeys.

    Searches the whole viewport for a distinctive, fixed-size reference patch.
    Configure once after loading the layout. Missing references use the usual
    recording UI; referenceScreenshot imports/redefines from a saved full
    viewport screenshot instead. Pick constant text or an icon from the popup.

    Two consecutive clear captures are required. A persistent popup, capture
    error, or changed page/layout/viewport prevents the pending input. The overall
    timeout includes capture and detection; waiting cannot be skipped.
    sendkeys checks before Home, Shift+End, each text chunk, and each Enter;
    a text chunk is sent as one keyboard.type call, not guarded per character.
    """
    global _click_guard
    timeout, interval = float(timeout), float(pollInterval)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Click guard timeout must be a positive finite number.")
    if not math.isfinite(interval) or interval <= 0 or interval >= timeout:
        raise ValueError("Click guard poll interval must be positive and less than timeout.")
    page = _require_page()
    if referenceScreenshot is not None:
        defineFrame(name, screenshot=referenceScreenshot)
    entry = _layout_entry(name, "frame")
    selected = _require_layout()
    matcher = _popup.PopupMatcher(loadFrame(entry["image"]), threshold=matchLevel)
    # Commit only a fully validated configuration. Failed reconfiguration must
    # never silently disable a previously enabled guard.
    _click_guard = dict(name=name, timeout=timeout, interval=interval,
                        matcher=matcher, layout=selected, page=page,
                        viewport=tuple(selected.viewport))
    _emit("Popup input guard enabled: %s; waiting up to %gs for two clear captures"
          % (name, timeout))


def disableClickGuard():
    """Explicitly disable the optional popup guard for mouse and keyboard input."""
    global _click_guard
    _click_guard = None


def _save_click_guard_timeout(shot):
    if shot is None:
        return
    try:
        path = os.path.join(_run_folder(), "click-guard-timeout-%s-%s.png"
                            % (time.strftime("%H%M%S"), time.time_ns()))
        shot.save(path)
        _emit("Saved popup timeout screenshot: %s" % path)
    except Exception as exc:
        _emit("Could not save popup timeout screenshot: %s" % exc)


def _wait_for_click_guard(action="click"):
    guard = _click_guard
    if guard is None:
        return
    deadline = time.monotonic() + guard["timeout"]
    clear_count = 0
    waiting = False
    last_shot = None

    def timed_out():
        _save_click_guard_timeout(last_shot)
        raise ClickGuardTimeout(
            "Popup guard %r did not confirm a clear screen within %gs; "
            "the pending %s was NOT sent. Earlier input, if any, is not replayed."
            % (guard["name"], guard["timeout"], action))

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out()
        if _page is not guard["page"] or _layout is not guard["layout"]:
            raise ClickGuardError("The page or layout changed after enabling the click guard; "
                                  "configure it again before sending input.")
        try:
            # Never use 0: Playwright interprets it as disabling the timeout.
            last_shot = viewportGrab(timeout=max(1, min(2000, int(remaining * 1000))))
            if last_shot.size != guard["viewport"]:
                raise ClickGuardError("Viewport changed during the popup check; "
                                      "restore the recorded layout size before sending input.")
            if time.monotonic() >= deadline:
                timed_out()
            found = guard["matcher"].locate(last_shot)
        except ClickGuardError:
            raise
        except Exception as exc:
            raise ClickGuardError("Could not check for popup %r; the pending %s was NOT sent: %s"
                                  % (guard["name"], action, exc)) from exc
        if time.monotonic() >= deadline:
            timed_out()
        if found is not None:
            clear_count = 0
            if not waiting:
                _emit("Popup %r detected; waiting for it to disappear (up to %gs)"
                      % (guard["name"], guard["timeout"]))
                waiting = True
        else:
            clear_count += 1
            if clear_count >= 2:
                # No logging or UI between the final fresh check and input.
                return
            if waiting:
                _emit("Popup %r appears clear; confirming with another fresh capture" % guard["name"])
        time.sleep(min(guard["interval"], max(0.0, deadline - time.monotonic())))


def click(x, y=None):
    """Click a named layout point, or legacy (x, y) viewport coordinates."""
    if isinstance(x, str) and y is None:
        entry = _layout_entry(x, "point")
        x, y = entry["x"], entry["y"]
    elif y is None:
        raise TypeError("click requires a layout name or both x and y")
    _wait_for_click_guard()
    _require_page().mouse.click(x, y)
    if _clicks_settle_time > 0:
        time.sleep(_clicks_settle_time)


def doubleClick(x, y=None):
    """Double-click a named layout point, or legacy (x, y) coordinates."""
    if isinstance(x, str) and y is None:
        entry = _layout_entry(x, "point")
        x, y = entry["x"], entry["y"]
    elif y is None:
        raise TypeError("doubleClick requires a layout name or both x and y")
    _wait_for_click_guard()
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

    When enableClickGuard() is configured, every Home, Shift+End, text chunk,
    and Enter waits for the popup to clear before it is sent. Each text chunk
    remains one keyboard.type operation. Already-sent input is never replayed
    if a later operation times out or fails.
    """
    page = _require_page()
    normalized = text.replace("\r\n", "\r").replace("\n", "\r")
    parts = normalized.split("\r")
    for i, part in enumerate(parts):
        if part and HomeShiftEndPrefix:
            _wait_for_click_guard(action="keyboard input")
            page.keyboard.press("Home")
            if delay:
                time.sleep(delay / 1000.0)
            _wait_for_click_guard(action="keyboard input")
            page.keyboard.press("Shift+End")
            if delay:
                time.sleep(delay / 1000.0)
        if part:
            _wait_for_click_guard(action="keyboard input")
            page.keyboard.type(part, delay=delay)
        if i < len(parts) - 1:
            _wait_for_click_guard(action="keyboard input")
            page.keyboard.press("Enter")


def viewportGrab(*, timeout=None):
    """Screenshot of the viewport as RGB; optional timeout is in milliseconds."""
    options = {"scale": "css"}
    if timeout is not None:
        options["timeout"] = timeout
    png = _require_page().screenshot(**options)
    return Image.open(io.BytesIO(png)).convert("RGB")


def viewportSize():
    """(width, height) of the page viewport in CSS pixels."""
    page = _require_page()
    return (int(page.evaluate("window.innerWidth")),
            int(page.evaluate("window.innerHeight")))


def _frame_and_full(x1, y1, x2, y2, *, timeout=None):
    """(cropped region, full viewport) taken from ONE screenshot, so the
    crop is guaranteed to be a piece of the returned full frame."""
    if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
        raise ValueError(
            "frameGrab needs 0 <= x1 < x2 and 0 <= y1 < y2, got (%s, %s, %s, %s)"
            % (x1, y1, x2, y2))
    shot = viewportGrab() if timeout is None else viewportGrab(timeout=timeout)
    if x2 > shot.width or y2 > shot.height:
        raise ValueError(
            "frame (%s, %s, %s, %s) reaches outside the %sx%s viewport"
            % (x1, y1, x2, y2, shot.width, shot.height))
    return shot.crop((int(x1), int(y1), int(x2), int(y2))), shot


def frameGrab(x1, y1=None, x2=None, y2=None):
    """Capture a named region, or legacy rectangle coordinates, as a Pillow image."""
    if isinstance(x1, str) and y1 is None and x2 is None and y2 is None:
        box = layoutBounds(x1)
        cropped, full = _frame_and_full(*box)
        _require_layout().check_viewport(full.size)
        return cropped
    if y1 is None or x2 is None or y2 is None:
        raise TypeError("frameGrab requires a layout name or four rectangle coordinates")
    return _frame_and_full(x1, y1, x2, y2)[0]


def grabOCRTextLine(x1, y1=None, x2=None, y2=None, *, tesseract_cmd=None):
    """Capture a named layout region and return its text as one string.

    Like frameGrab(), accepts either a region name or four rectangle
    coordinates in CSS pixels. A missing named region uses the normal layout
    definition dialog; every call captures fresh pixels after it is defined.

    Uses local Tesseract with English data in single-line mode. Pass
    tesseract_cmd to select its executable, otherwise PATH and standard Windows
    install locations are searched. Checks OCR availability before capture or
    opening a definition dialog. No browser clicks or keys are sent.

    Leading/trailing whitespace is removed; internal spacing, signs, decimal
    points and other recognized characters are preserved. This function does
    not parse or validate values. Raises OCRError for OCR setup/execution
    failures, no text, or unexpected multiple lines. Layout/capture errors
    propagate just as they do in frameGrab().
    """
    reader = _ocr.TesseractReader(tesseract_cmd)
    pixels = frameGrab(x1, y1, x2, y2)
    text = reader(pixels).strip()
    if not text:
        raise OCRError("OCR returned no text for the selected rectangle.")
    if len(text.splitlines()) != 1:
        raise OCRError("OCR returned multiple lines; select a rectangle containing one text line.")
    return text


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


def _pixel_inspector(parent, expected, actual):
    """Zoomable side-by-side pixel view opened from the diff viewer.

    Shows both frames blown up with nearest-neighbour pixels (grid lines
    from 8x), scrolling in sync.  Clicking a pixel on either side
    highlights the same location on both and reports the two RGB values
    and their max channel difference.  Returns the Toplevel window with
    update_actual(image) and set_live_status(text) methods. Updates preserve
    the selected pixel, zoom and scroll positions; they never resize a capture.
    """
    import tkinter as tk
    from math import isqrt
    from PIL import ImageTk

    expected = expected.convert("RGB")
    actual = actual.convert("RGB")
    if actual.size != expected.size:
        actual = actual.resize(expected.size, Image.LANCZOS)
    w, h = expected.size
    # Keep live redraw allocations bounded when inspecting a large capture.
    # A capture already above the budget stays at its original 1x size.
    max_zoom = max(1, min(64, isqrt(4_000_000 // max(w * h, 1))))

    top = tk.Toplevel(parent)
    top.title("Pixel inspector")
    top.attributes("-topmost", True)

    state = {"zoom": max(1, min(max_zoom, 32, 480 // max(w, 1), 320 // max(h, 1))),
             "sel": None}
    photos = {}

    info = tk.Label(top, text="Click a pixel to inspect it",
                    font=("Consolas", 10))
    info.pack(padx=10, pady=(10, 2))
    live_status = tk.Label(top, text="", font=("Segoe UI", 9))
    live_status.pack(padx=10, pady=(0, 2))

    grid_frame = tk.Frame(top)
    grid_frame.pack(padx=10, pady=4)
    tk.Label(grid_frame, text="Expected (saved capture)",
             font=("Segoe UI", 10)).grid(row=0, column=0)
    tk.Label(grid_frame, text="Actual (screen now)",
             font=("Segoe UI", 10)).grid(row=0, column=1)
    c_exp = tk.Canvas(grid_frame, highlightthickness=1,
                      highlightbackground="#888888")
    c_act = tk.Canvas(grid_frame, highlightthickness=1,
                      highlightbackground="#888888")
    c_exp.grid(row=1, column=0, padx=(0, 6))
    c_act.grid(row=1, column=1, padx=(6, 0))
    ys = tk.Scrollbar(grid_frame, orient="vertical",
                      command=lambda *a: (c_exp.yview(*a), c_act.yview(*a)))
    ys.grid(row=1, column=2, sticky="ns")
    xs = tk.Scrollbar(grid_frame, orient="horizontal",
                      command=lambda *a: (c_exp.xview(*a), c_act.xview(*a)))
    xs.grid(row=2, column=0, columnspan=2, sticky="ew")
    c_exp.config(yscrollcommand=ys.set, xscrollcommand=xs.set)
    c_act.config(yscrollcommand=ys.set, xscrollcommand=xs.set)

    def draw_sel():
        z = state["zoom"]
        for canvas in (c_exp, c_act):
            canvas.delete("sel")
        if state["sel"] is None:
            return
        px, py = state["sel"]
        for canvas in (c_exp, c_act):
            canvas.create_rectangle(px * z, py * z, (px + 1) * z, (py + 1) * z,
                                    outline="#ffee00", width=2, tags="sel")

    def refresh_pixel_info():
        if state["sel"] is None:
            return
        px, py = state["sel"]
        e_rgb = expected.getpixel((px, py))
        a_rgb = actual.getpixel((px, py))
        diff = max(abs(e_rgb[i] - a_rgb[i]) for i in range(3))
        info.config(text="Pixel (%d, %d)   expected RGB %s   actual RGB %s   "
                         "max channel diff %d" % (px, py, e_rgb, a_rgb, diff))

    def redraw(preserve_scroll=False):
        offsets = [(canvas.xview()[0], canvas.yview()[0])
                   for canvas in (c_exp, c_act)] if preserve_scroll else None
        z = state["zoom"]
        zw, zh = w * z, h * z
        photos["exp"] = ImageTk.PhotoImage(
            expected.resize((zw, zh), Image.NEAREST), master=top)
        photos["act"] = ImageTk.PhotoImage(
            actual.resize((zw, zh), Image.NEAREST), master=top)
        for canvas, key in ((c_exp, "exp"), (c_act, "act")):
            canvas.delete("all")
            canvas.create_image(0, 0, image=photos[key], anchor="nw")
            if z >= 8:
                for gx in range(0, zw + 1, z):
                    canvas.create_line(gx, 0, gx, zh, fill="#666666")
                for gy in range(0, zh + 1, z):
                    canvas.create_line(0, gy, zw, gy, fill="#666666")
            canvas.config(scrollregion=(0, 0, zw, zh),
                          width=min(zw, 430), height=min(zh, 320))
        if offsets is not None:
            for canvas, (x_offset, y_offset) in zip((c_exp, c_act), offsets):
                canvas.xview_moveto(x_offset)
                canvas.yview_moveto(y_offset)
        draw_sel()
        refresh_pixel_info()
        zoom_label.config(text="zoom %dx" % z)

    def on_click(event, canvas):
        z = state["zoom"]
        px = int(canvas.canvasx(event.x) // z)
        py = int(canvas.canvasy(event.y) // z)
        if not (0 <= px < w and 0 <= py < h):
            return
        state["sel"] = (px, py)
        refresh_pixel_info()
        draw_sel()

    c_exp.bind("<Button-1>", lambda ev: on_click(ev, c_exp))
    c_act.bind("<Button-1>", lambda ev: on_click(ev, c_act))

    def set_zoom(direction):
        z = state["zoom"]
        nz = min(max_zoom, z * 2) if direction > 0 else max(1, z // 2)
        if nz != z:
            state["zoom"] = nz
            redraw()

    controls = tk.Frame(top)
    controls.pack(pady=(4, 10))
    tk.Button(controls, text="Zoom in", font=("Segoe UI", 10, "bold"),
              command=lambda: set_zoom(1), padx=10).pack(side="left", padx=4)
    tk.Button(controls, text="Zoom out", font=("Segoe UI", 10, "bold"),
              command=lambda: set_zoom(-1), padx=10).pack(side="left", padx=4)
    zoom_label = tk.Label(controls, text="", font=("Segoe UI", 10))
    zoom_label.pack(side="left", padx=8)
    tk.Button(controls, text="Close", font=("Segoe UI", 10, "bold"),
              command=top.destroy, padx=14).pack(side="left", padx=4)

    def update_actual(image):
        nonlocal actual
        if image.size != expected.size:
            raise ValueError("Live pixel capture size %s does not match expected size %s"
                             % (image.size, expected.size))
        actual = image.convert("RGB")
        redraw(preserve_scroll=True)

    top.update_actual = update_actual
    top.set_live_status = lambda text: live_status.config(text=text)
    redraw()
    top.lift()
    return top


def _grab_live_compare(box, expected_size, expected_viewport=None):
    """One bounded screenshot for an alignment preview; never rescale geometry."""
    actual, full = _frame_and_full(*box, timeout=2000)
    if expected_viewport is not None and full.size != tuple(expected_viewport):
        raise ValueError(
            "Viewport is %dx%d; restore %dx%d to align this layout."
            % (*full.size, *expected_viewport))
    if actual.size != tuple(expected_size):
        raise ValueError("Capture dimensions differ from the saved reference.")
    return actual, full


class _LiveCompareRefresh:
    """Synchronous refresh state; poll outside Tk callbacks on the owning thread."""

    def __init__(self, capture, on_frame, on_status, interval=1.0, clock=None):
        self.capture = capture
        self.on_frame = on_frame
        self.on_status = on_status
        self.interval = interval
        self.clock = clock or time.monotonic
        self.enabled = True
        self.closed = False
        self.busy = False
        self.pending = True
        self.deadline = 0.0
        self.error = None
        self.updated = None

    def _publish_status(self):
        if self.error:
            mode = "Live refresh" if self.enabled else "Paused"
            self.on_status("%s failed: %s — showing the last captured image"
                           % (mode, self.error), stale=True)
        else:
            mode = "Live — updates every 1 second" if self.enabled else "Paused"
            stamp = "last capture %s" % self.updated if self.updated else "waiting for first refresh"
            self.on_status("%s; %s" % (mode, stamp), stale=False)

    def set_enabled(self, enabled):
        self.enabled = bool(enabled)
        self.pending = self.enabled
        self._publish_status()

    def request_refresh(self):
        self.pending = True

    def close(self):
        self.closed = True
        self.pending = False

    def poll(self):
        if self.closed or self.busy:
            return False
        if not self.pending and (not self.enabled or self.clock() < self.deadline):
            return False
        self.pending = False
        self.busy = True
        try:
            try:
                actual, full = self.capture()
            except Exception as exc:
                # Keep multi-line browser traces from expanding the dialog off-screen.
                detail = str(exc).strip()
                self.error = detail.splitlines()[0][:240] if detail else type(exc).__name__
                self._publish_status()
                return False
            self.on_frame(actual, full)
            self.error = None
            self.updated = time.strftime("%H:%M:%S")
            self._publish_status()
            return True
        finally:
            self.busy = False
            # Cadence begins after completion; slow captures never build a queue.
            self.deadline = self.clock() + self.interval


def _diff_viewer(expected, actual, score, matchLevel, box=None, full=None,
                 refresh_callback=None):
    """Live alignment preview. Closing returns to the alarm, never resumes.

    A refresh callback supplies (crop, full screenshot) on the Playwright
    owning thread. Tk callbacks only change UI state; browser calls happen
    between event-pump iterations. Every window is destroyed on exit.
    """
    import tkinter as tk
    from PIL import ImageTk

    expected = expected.convert("RGB")
    actual = actual.convert("RGB")
    if actual.size != expected.size:
        raise ValueError("Comparison capture dimensions differ from the reference.")

    root = tk.Tk()
    controller = None
    try:
        root.title("Compare differences — live alignment")
        root.attributes("-topmost", True)
        scale = min(1.0, 0.44 * root.winfo_screenwidth() / expected.width,
                    0.60 * root.winfo_screenheight() / expected.height)
        current = {"actual": actual, "full": full, "score": score, "diff_pct": 0.0}
        context = {"win": None}
        pixels = {"win": None}
        running = {"open": True}
        latest_status = {"text": "Captured image from the failed verification", "stale": False}

        def alive(window):
            try:
                return window is not None and bool(window.winfo_exists())
            except tk.TclError:
                return False

        def fit(im):
            if scale >= 1.0:
                return im
            return im.resize((max(1, int(im.width * scale)),
                              max(1, int(im.height * scale))), Image.LANCZOS)

        tk.Label(root, text="Adjust the application window while watching the live capture.",
                 font=("Segoe UI", 11)).pack(padx=12, pady=(10, 2))
        score_label = tk.Label(root, font=("Segoe UI", 12, "bold"))
        score_label.pack(padx=12, pady=4)
        status_label = tk.Label(root, font=("Segoe UI", 10), wraplength=850)
        status_label.pack(padx=12, pady=2)
        row = tk.Frame(root)
        row.pack(padx=10, pady=4)
        panes = []
        for column, caption in ((0, "Expected (saved reference)"),
                                (1, "Actual (latest captured image)")):
            pane = tk.Frame(row)
            pane.grid(row=0, column=column, padx=8)
            tk.Label(pane, text=caption, font=("Segoe UI", 11)).pack()
            label = tk.Label(pane)
            label.pack()
            panes.append(label)

        photos = {}
        highlight_on = tk.BooleanVar(master=root, value=True)

        def render_score():
            if latest_status["stale"]:
                score_label.config(text="No current comparison — last captured image is shown",
                                   fg="#b00020")
            else:
                matched = current["score"] >= matchLevel
                score_label.config(
                    text="%s in captured image — similarity %.4f (needs %s); %.1f%% differs"
                         % ("MATCH" if matched else "MISMATCH", current["score"],
                            matchLevel, current["diff_pct"]),
                    fg="#176b35" if matched else "#b00020")

        def render():
            mask = _diff_mask(expected, current["actual"])
            current["diff_pct"] = 100.0 * mask.histogram()[255] / (mask.width * mask.height)
            left, right = expected, current["actual"]
            if highlight_on.get():
                left, right = _highlight(left, mask), _highlight(right, mask)
            photos["expected"] = ImageTk.PhotoImage(fit(left), master=root)
            photos["actual"] = ImageTk.PhotoImage(fit(right), master=root)
            panes[0].config(image=photos["expected"])
            panes[1].config(image=photos["actual"])
            render_score()

        def show_status(text, stale=False):
            latest_status.update(text=text, stale=stale)
            status_label.config(text=text, fg="#b00020" if stale else "#444444")
            render_score()
            if alive(pixels["win"]):
                pixels["win"].set_live_status(text)
            if alive(context["win"]):
                context["status"].config(text=text, fg="#b00020" if stale else "#444444")

        def update_context():
            if not alive(context["win"]) or current["full"] is None:
                return
            top = context["win"]
            frame = current["full"]
            cscale = min(1.0, 0.88 * top.winfo_screenwidth() / frame.width,
                         0.72 * top.winfo_screenheight() / frame.height)
            size = (max(1, int(frame.width * cscale)), max(1, int(frame.height * cscale)))
            disp = frame if cscale >= 1.0 else frame.resize(size, Image.LANCZOS)
            top._photo = ImageTk.PhotoImage(disp, master=top)
            canvas = context["canvas"]
            canvas.itemconfigure(context["image"], image=top._photo)
            canvas.config(width=disp.width, height=disp.height)
            canvas.coords(context["rect"], *[int(round(v * cscale)) for v in box])
            context["label"].config(
                text="Blinking box: (%d, %d)-(%d, %d) in the %dx%d viewport"
                     % (*box, frame.width, frame.height))

        def open_context():
            if alive(context["win"]):
                context["win"].lift()
                return
            if current["full"] is None or box is None:
                return
            top = tk.Toplevel(root)
            context["win"] = top
            top.title("Live capture region in full frame")
            top.attributes("-topmost", True)
            context["label"] = tk.Label(top, font=("Segoe UI", 11))
            context["label"].pack(padx=8, pady=(8, 4))
            context["status"] = tk.Label(top, text=latest_status["text"],
                                         font=("Segoe UI", 10), wraplength=850,
                                         fg="#b00020" if latest_status["stale"] else "#444444")
            context["status"].pack(padx=8, pady=2)
            canvas = context["canvas"] = tk.Canvas(top, highlightthickness=0)
            canvas.pack(padx=8, pady=4)
            context["image"] = canvas.create_image(0, 0, anchor="nw")
            context["rect"] = canvas.create_rectangle(0, 0, 1, 1, outline="#ff2020", width=3)
            context["blink"] = True
            context["next_blink"] = time.monotonic() + 0.4
            tk.Button(top, text="Close", font=("Segoe UI", 11, "bold"),
                      command=top.destroy, padx=16, pady=4).pack(pady=(4, 10))
            update_context()

        def blink_context():
            if alive(context["win"]) and time.monotonic() >= context["next_blink"]:
                context["blink"] = not context["blink"]
                context["canvas"].itemconfigure(
                    context["rect"], outline="#ff2020" if context["blink"] else "#ffffff")
                context["next_blink"] = time.monotonic() + 0.4

        def open_pixels():
            if alive(pixels["win"]):
                pixels["win"].lift()
                return
            pixels["win"] = _pixel_inspector(root, expected, current["actual"])
            pixels["win"].set_live_status(latest_status["text"])

        def accept_frame(fresh, full_frame):
            if fresh.size != expected.size:
                raise ValueError("Live capture dimensions differ from the saved reference.")
            current.update(actual=fresh.convert("RGB"), full=full_frame,
                           score=frameSimilarity(expected, fresh))
            render()
            if alive(pixels["win"]):
                pixels["win"].update_actual(current["actual"])
            update_context()

        tk.Checkbutton(root, text="Highlight differences (red)", variable=highlight_on,
                       command=render, font=("Segoe UI", 11)).pack(pady=4)

        if refresh_callback is not None:
            controller = _LiveCompareRefresh(refresh_callback, accept_frame, show_status)
            controls = tk.Frame(root)
            controls.pack(pady=4)

            def toggle_live():
                controller.set_enabled(not controller.enabled)
                pause_button.config(text="Pause live refresh" if controller.enabled
                                    else "Resume live refresh")

            pause_button = tk.Button(controls, text="Pause live refresh",
                                     font=("Segoe UI", 11), command=toggle_live,
                                     padx=12, pady=4)
            pause_button.pack(side="left", padx=4)
            tk.Button(controls, text="Refresh now", font=("Segoe UI", 11),
                      command=controller.request_refresh, padx=12, pady=4).pack(side="left", padx=4)

        tk.Button(root, text="View pixels", font=("Segoe UI", 11),
                  command=open_pixels, padx=12, pady=4).pack(pady=4)
        if full is not None and box is not None:
            tk.Button(root, text="Show current capture inside full frame grab",
                      font=("Segoe UI", 11), command=open_context,
                      padx=12, pady=4).pack(pady=4)

        def request_close():
            running["open"] = False

        tk.Label(root, text="When aligned, go Back to alarm and choose Try compare again.",
                 font=("Segoe UI", 10)).pack(padx=12, pady=(4, 0))
        tk.Button(root, text="Back to alarm", font=("Segoe UI", 12, "bold"),
                  command=request_close, padx=20, pady=6).pack(pady=(4, 12))
        root.protocol("WM_DELETE_WINDOW", request_close)
        root.bind("<Escape>", lambda event: request_close())
        render()
        show_status(latest_status["text"])
        root.update_idletasks()
        x = max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)
        y = max(0, (root.winfo_screenheight() - root.winfo_height()) // 4)
        root.geometry("+%d+%d" % (x, y))
        root.lift()
        root.focus_force()

        # No screenshots in Tk callbacks or worker threads: Playwright remains
        # on the thread/greenlet that owns its connection, with no reentrancy.
        while running["open"] and alive(root):
            root.update()
            if not running["open"] or not alive(root):
                break
            if controller is not None:
                controller.poll()
            blink_context()
            time.sleep(0.03)
    finally:
        if controller is not None:
            controller.close()
        try:
            root.destroy()
        except tk.TclError:
            pass


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


class _VerifyPopup:
    """Countdown window shown while verifyFrame() runs a delay/retry cycle.

    Shows "Verifying frame / <name> / Try n of m in Xs" plus the last
    similarity score.  Buttons: "Try now" skips the current countdown,
    "Abort verify" gives up on this screen test (verifyFrame returns
    False), "Abort script" exits with code 2.  Closing the window hides
    it and the verify continues silently.
    """

    def __init__(self, name):
        import tkinter as tk

        self.action = None
        self.hidden = False
        self._timer = None
        bg = "#0f5b5b"
        root = self.root = tk.Tk()
        root.title("Verifying frame")
        root.configure(bg=bg)
        root.attributes("-topmost", True)
        tk.Label(root, text="Verifying frame", font=("Segoe UI", 16, "bold"),
                 fg="white", bg=bg).pack(padx=40, pady=(18, 2))
        tk.Label(root, text=name, font=("Segoe UI", 24, "bold"),
                 fg="#9fe8e8", bg=bg).pack(padx=40, pady=2)
        self._status = tk.Label(root, text="", font=("Segoe UI", 15),
                                fg="white", bg=bg)
        self._status.pack(padx=40, pady=4)
        self._timeout = tk.Label(root, text="", font=("Segoe UI", 11),
                                 fg="#cfe8e8", bg=bg)
        self._timeout.pack(padx=40, pady=0)
        self._score = tk.Label(root, text="", font=("Segoe UI", 11),
                               fg="#cfe8e8", bg=bg)
        self._score.pack(padx=40, pady=(0, 6))
        row = tk.Frame(root, bg=bg)
        row.pack(padx=30, pady=(4, 18))
        for label, action in (("Try now", "trynow"),
                              ("Abort verify", "abortverify"),
                              ("Abort script", "abortscript")):
            tk.Button(row, text=label, font=("Segoe UI", 11, "bold"),
                      command=lambda a=action: self._pick(a),
                      padx=12, pady=6).pack(side="left", padx=6)
        root.protocol("WM_DELETE_WINDOW", self._hide)
        root.update_idletasks()
        x = (root.winfo_screenwidth() - root.winfo_width()) // 2
        y = (root.winfo_screenheight() - root.winfo_height()) // 3
        root.geometry("+%d+%d" % (x, y))
        root.lift()  # informational: no focus stealing

    def _pick(self, action):
        self.action = action
        self._cancel_timer()
        self.root.quit()

    def _hide(self):
        self.hidden = True
        self._cancel_timer()
        try:
            self.root.destroy()
        except Exception:
            pass
        _emit("[%s] Verify window hidden -- continuing silently"
              % time.strftime("%H:%M:%S"))

    def _cancel_timer(self):
        if self._timer is not None:
            try:
                self.root.after_cancel(self._timer)
            except Exception:
                pass
            self._timer = None

    def countdown(self, seconds, attempt, total):
        """Pump the window for `seconds` before attempt n of m.  Returns
        'trynow', 'abortverify', 'abortscript', or None (time elapsed)."""
        deadline = time.monotonic() + max(0.0, seconds)
        if self.hidden:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            return None

        def tick():
            self._timer = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.root.quit()
                return
            self._status.config(text="Try %d of %d in %s"
                                % (attempt, total, _fmt_remaining(remaining)))
            until_timeout = remaining + (total - attempt) * seconds
            self._timeout.config(text="(%d seconds until timeout)"
                                 % int(until_timeout + 0.999))
            self._timer = self.root.after(150, tick)

        try:
            self._status.config(text="Try %d of %d" % (attempt, total))
            self._timer = self.root.after(0, tick)
            self.root.mainloop()
        except Exception:
            self.hidden = True
        self._cancel_timer()
        action = self.action
        self.action = None
        if self.hidden and action is None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        return action

    def note_grab(self, attempt, total):
        if self.hidden:
            return
        try:
            self._status.config(text="Try %d of %d -- checking..." % (attempt, total))
            self.root.update()
        except Exception:
            self.hidden = True

    def note_score(self, text):
        if self.hidden:
            return
        try:
            self._score.config(text=text)
        except Exception:
            self.hidden = True

    def close(self):
        self._cancel_timer()
        if not self.hidden:
            try:
                self.root.destroy()
            except Exception:
                pass
        self.hidden = True


def verifyFrame(baselinePath, box=None, matchLevel=0.98, message=None, delay=0, retrycount=0,
                fallthru=False):
    """Compare a named layout frame, or a legacy explicit PNG and rectangle.

    Named form: verifyFrame("focus-ready", matchLevel=0.99, delay=10,
    retrycount=24). Only geometry and the reference PNG come from the layout;
    thresholds, timing, messages, and mismatch handling remain script options.
    A missing frame asks the operator to establish the expected state and
    explicitly accept a preview before saving. Canceling definition stops.

    box is (x1, y1, x2, y2) -- the region the capture was taken from.  On a
    mismatch a loud alarm is raised offering the operator four choices
    (unless fallthru=True, see below):

      1. Try compare again -- e.g. after manually putting the target page
         back into the right state
      2. Show differences  -- live side-by-side alignment viewer, refreshing
         the capture, score, highlights, pixel inspector and full-frame
         context about once per second. Pause/manual refresh are available.
         The saved reference stays fixed. Return to the alarm and choose
         Try compare again to recheck; live preview never resumes execution.
         Saves diff PNGs instead when there is no display.
      3. Skip and continue -- accept the mismatch and resume the script
      4. Stop the script   -- abort immediately (exit code 2)

    delay is seconds to sleep before each grab (0 = grab immediately);
    retrycount is how many tries before raising the alarm (0 and 1 both
    mean a single try).  With e.g. delay=10, retrycount=90 a screen test
    becomes a poll: check every 10 seconds and only alarm if the screen
    never matched within ~15 minutes -- usually better than a long fixed
    wait().  Whenever a delay or retries are in play, a quiet countdown
    window shows "Verifying frame / <name> / Try n of m in Xs" with the
    last similarity score and three buttons: "Try now" (skip the current
    countdown), "Abort verify" (give up on this test; returns False), and
    "Abort script" (exit code 2); closing the window hides it and the
    cycle continues silently.  Instant single-try verifies show no
    window.  A match on a later attempt logs which attempt succeeded, and
    the give-up alarm reports the try count.  The operator's "Try compare
    again" button reruns the whole cycle.

    fallthru=True turns the give-up into a quiet outcome: when the frames
    still do not match after the last try, no alarm is raised -- a line is
    logged and False is returned for the caller to act on (branch, retry
    something else, alarm itself...).  The countdown window and its
    buttons behave exactly as before while the tries are running.

    Returns True when the frames matched (possibly after retries), False
    when the operator chose to skip / abort the verify, or -- with
    fallthru=True -- when the tries ran out without a match.
    """
    if not 0.0 <= matchLevel <= 1.0:
        raise ValueError("matchLevel must be between 0.0 and 1.0")
    delay = max(0.0, float(delay))
    attempts = max(1, int(retrycount))
    named = box is None
    layout_name = baselinePath if named else None
    if message is None:
        message = "Screen does not match %s" % baselinePath
    if named:
        entry = _layout_entry(layout_name, "frame")
        baselinePath, box = entry["image"], tuple(entry["box"])
    baseline = loadFrame(baselinePath)
    x1, y1, x2, y2 = box
    region = (x1, y1, x2, y2)
    base_name = os.path.basename(str(baselinePath))
    name_match = re.match(
        r"^capture-.+-([A-Za-z_][A-Za-z0-9_]*)-\d+,\d+,\d+,\d+\.png$", base_name)
    display_name = layout_name if named else (name_match.group(1) if name_match else base_name)
    while True:
        popup = None
        if delay > 0 or attempts > 1:
            try:
                popup = _VerifyPopup(display_name)
            except Exception:
                popup = None
        outcome = None
        try:
            for attempt in range(1, attempts + 1):
                action = None
                if popup is not None:
                    action = popup.countdown(delay, attempt, attempts)
                elif delay:
                    time.sleep(delay)
                if action in ("abortverify", "abortscript"):
                    outcome = action
                    break
                if popup is not None:
                    popup.note_grab(attempt, attempts)
                if named:
                    layoutViewport()
                fresh, full = _frame_and_full(x1, y1, x2, y2)
                if named:
                    _require_layout().check_viewport(full.size)
                score = frameSimilarity(baseline, fresh)
                if score >= matchLevel:
                    if attempt > 1:
                        _emit("[%s] Screen test matched on attempt %d/%d (similarity %.4f)"
                              % (time.strftime("%H:%M:%S"), attempt, attempts, score))
                    outcome = True
                    break
                if popup is not None:
                    popup.note_score("last similarity %.4f (needs at least %s)"
                                     % (score, matchLevel))
        finally:
            if popup is not None:
                popup.close()
        if outcome is True:
            return True
        if outcome == "abortverify":
            _emit("[%s] Verify aborted by operator -- continuing"
                  % time.strftime("%H:%M:%S"))
            return False
        if outcome == "abortscript":
            _emit("[%s] Script stopped by operator" % time.strftime("%H:%M:%S"))
            sys.exit(2)
        if attempts > 1:
            detail = ("%s  (similarity %.4f after %d tries, needs at least %s)"
                      % (message, score, attempts, matchLevel))
        else:
            detail = "%s  (similarity %.4f, needs at least %s)" % (message, score, matchLevel)
        if fallthru:
            _emit("[%s] Screen test '%s' did not match -- falling through: %s"
                  % (time.strftime("%H:%M:%S"), display_name, detail))
            return False
        while True:
            choice = alarm(detail, buttons=("Try compare again", "Show differences",
                                            "Skip and continue", "Stop the script"))
            if choice != 1:
                break
            try:
                live_viewport = tuple(_require_layout().viewport) if named else None

                def refresh_capture():
                    return _grab_live_compare(region, baseline.size, live_viewport)

                _diff_viewer(baseline, fresh, score, matchLevel, box=region, full=full,
                             refresh_callback=refresh_capture)
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
    closed = {"done": False}

    def close(outcome):
        if closed["done"]:
            return
        closed["done"] = True
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

    root.protocol("WM_DELETE_WINDOW", lambda: close("dismissed"))  # X = hide, keep waiting
    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry("+%d+%d" % (x, y))
    root.lift()  # deliberately no focus_force: informational, not an alarm
    tick()
    if not closed["done"]:
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

    With screenshotOnInfo(True) active, each info line also saves a full
    screenshot; with pauseOnInfo(True) active, it pauses the script until
    the operator chooses how to proceed."""
    _emit("[%s] %s" % (time.strftime("%H:%M:%S"), message))
    if _shot_dir is not None:
        _snap_info(message)
    if _pause_on_info:
        _step_pause(message)


def screenshotOnInfo(enabled, folder=None):
    """Save a full screenshot of the viewport on every info() call.

    By default PNGs go to this run's runs\\<yymmdd hhmmss>\\ folder
    (created next to the running script and shared with logging, so one
    run's log and screenshots stay together).  Pass folder=... to use a
    fixed location instead (created if needed; a relative path resolves
    next to the script).  Files are named
    "yymmdd hhmmss-<info message>.png" -- a visual flight recorder of the
    whole run.  Info lines that happen before the browser is connected
    are skipped quietly.  Mind the disk: a long run at a big viewport
    adds up fast.
    """
    global _shot_dir, _shot_warned
    if not enabled:
        _shot_dir = None
        _emit("[%s] Info screenshots off" % time.strftime("%H:%M:%S"))
        return
    if folder is None:
        folder = _run_folder()
    if not os.path.isabs(folder):
        base = sys.argv[0] if sys.argv and sys.argv[0] else "."
        folder = os.path.join(os.path.dirname(os.path.abspath(base)), folder)
    os.makedirs(folder, exist_ok=True)
    _shot_dir = folder
    _shot_warned = False
    _emit("[%s] Info screenshots on -> %s" % (time.strftime("%H:%M:%S"), folder))


def _snap_info(message):
    global _shot_warned
    if _page is None:
        return  # not connected yet -- nothing to grab
    try:
        shot = viewportGrab()
        banned = set('\\/:*?"<>|')
        safe = "".join("_" if (c in banned or ord(c) < 32) else c
                       for c in message).strip()[:80].rstrip(" .")
        if not safe:
            safe = "info"
        stamp = time.strftime("%y%m%d %H%M%S")
        path = os.path.join(_shot_dir, "%s-%s.png" % (stamp, safe))
        n = 2
        while os.path.exists(path):
            path = os.path.join(_shot_dir, "%s-%s (%d).png" % (stamp, safe, n))
            n += 1
        shot.save(path)
    except Exception as e:
        if not _shot_warned:
            _shot_warned = True
            _emit("[%s] (info screenshot failed: %s)" % (time.strftime("%H:%M:%S"), e))


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

    psl.logging(True) writes <scriptname>.log into this run's
    runs\\<yymmdd hhmmss>\\ folder (created next to the running script and
    shared with screenshotOnInfo, so one run's log and screenshots stay
    together) and mirrors info() lines, alarm lines, operator answers and
    error tracebacks into it.  psl.logging(False) turns it off.  path
    overrides the file location (opened in append mode either way).
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
        path = os.path.join(_run_folder(),
                            os.path.splitext(os.path.basename(base))[0] + ".log")
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
    """Alarm on uncaught errors; allow Ctrl+C to exit without opening a dialog."""
    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            _emit("Script interrupted by operator (Ctrl+C); stopping without an alarm")
            return
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


def checkViewport(width=None, height=None):
    """Check the active layout's viewport, offering to restore its saved size.

    With no arguments, a mismatch offers Restore saved size, Check again,
    or Stop the script. Restoration uses setViewport, then checks fresh
    dimensions. There is no bypass and saved layout data is never changed.
    A new layout binds its initial size as before. Per-action viewport
    checks still raise immediately if the browser changes during a sequence.

    With explicit width and height, retain the legacy recorded-size check.

    Recorded coordinates only line up when the browser window (and zoom)
    match record time, so a size mismatch would silently click the wrong
    places.  The alarm offers: Fix window size (resize the browser via
    setViewport to match the recording), Check again, Continue anyway, or
    Stop the script (exit code 2).
    """
    if width is None and height is None:
        selected = _require_layout()
        restore_error = None
        while True:
            try:
                selected.check_viewport(viewportSize())
            except _layouts.ViewportMismatchError as exc:
                message = (
                    "%s\n\n'Restore saved size' resizes the browser to match this "
                    "layout. 'Check again' checks after you adjust the window. "
                    "The script will continue only when the dimensions match."
                    % exc)
            else:
                return selected.viewport
            if restore_error is not None:
                message += "\n\nLast restore attempt failed: %s" % restore_error
            choice = alarm(message, buttons=(
                "Restore saved size", "Check again", "Stop the script"))
            if choice == 0:
                try:
                    setViewport(*selected.viewport)
                except Exception as exc:
                    restore_error = str(exc)
                    _emit("Could not restore saved viewport: %s" % exc)
                else:
                    restore_error = None
            elif choice == 1:
                _emit("Re-checking layout viewport...")
            else:
                _emit("Script stopped by operator")
                sys.exit(2)
    if width is None or height is None:
        raise TypeError("checkViewport requires both width and height, or neither")
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
