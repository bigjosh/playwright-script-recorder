r"""playwrightscriptrecord -- interactively record a playwrightscriptlib script.

Run it, answer the prompts, and it writes a runnable Python script step by
step while performing each action live in the connected browser.  The script
file is flushed after every recorded action, so nothing is lost if recording
is aborted.
"""

import keyword
import os
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

from PIL import Image

import playwrightscriptlib as psl

MENU = """
What should we do next?
1. Click
2. Double Click
3. Send Keys
4. Screen Test (capture an area now, alert on replay if it changed)
5. Wait
6. End
"""

# capture-<scriptbase>-<capturename>-<x1>,<y1>,<x2>,<y2>.png
# (capturename is an identifier, so parsing from the right is unambiguous
# even when the script base name itself contains hyphens)
CAPTURE_RE = re.compile(
    r"^capture-(.+)-([A-Za-z_][A-Za-z0-9_]*)-(\d+),(\d+),(\d+),(\d+)\.png$")


def capture_filename(script_base, name, box):
    return "capture-%s-%s-%d,%d,%d,%d.png" % ((script_base, name) + tuple(box))


def list_captures(script_dir, script_base=None, name=None):
    """[(filename, name, (x1, y1, x2, y2), scriptbase), ...] found on disk."""
    out = []
    for fn in sorted(os.listdir(script_dir)):
        m = CAPTURE_RE.match(fn)
        if not m:
            continue
        if script_base is not None and m.group(1) != script_base:
            continue
        if name is not None and m.group(2) != name:
            continue
        out.append((fn, m.group(2),
                    (int(m.group(3)), int(m.group(4)),
                     int(m.group(5)), int(m.group(6))), m.group(1)))
    return out

DEBUG_URL_HELP = """\
The browser must already be running with its remote debug port open, e.g.:

  chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\temp\\chrome-debug

(the separate --user-data-dir is required when Chrome is already running
without the flag).  Then enter the stable form  http://127.0.0.1:9222  --
or paste the webSocketDebuggerUrl shown at http://127.0.0.1:9222/json/version
(note: that ws:// URL changes every time Chrome restarts).  Prefer 127.0.0.1
over "localhost": localhost can resolve to IPv6, which Chrome does not bind.
"""


class _WriterBase:
    """Shared codegen: turns an action into its comment/info/code block."""

    def action(self, comment, info_text, *code_lines):
        message = info_text + (" -- " + comment if comment else "")
        body = []
        if comment:
            body.append("# " + comment)
        body.append("psl.info(%r)" % message)
        body.extend(code_lines)
        self.note(*body)

    def finish(self):
        self.note("psl.info('Script finished')")


class ScriptWriter(_WriterBase):
    """Writes the recorded script to a file, flushing after every step."""

    def __init__(self, path):
        self.path = path
        self._f = open(path, "w", encoding="utf-8")

    def _line(self, text=""):
        self._f.write(text + "\n")
        self._f.flush()

    def header(self, lines):
        for text in lines:
            self._line(text)

    def note(self, *lines):
        self._line()
        for text in lines:
            self._line(text)

    def finish(self):
        _WriterBase.finish(self)
        self._f.close()


class ConsoleWriter(_WriterBase):
    """Immediate mode: prints each code block to the console; nothing saved."""

    path = None
    _RULE = "-" * 46

    def _block(self, lines, title="code"):
        print()
        print(("----- %s " % title).ljust(len(self._RULE), "-"))
        for text in lines:
            print(text)
        print(self._RULE)

    def header(self, lines):
        self._block(lines, "script header (copy this first for a runnable file)")

    def note(self, *lines):
        self._block(lines)

    def finish(self):
        _WriterBase.finish(self)
        print("\nImmediate mode: nothing was saved.")


def _run_picker(img, title, mode):
    """Show img fitted to the local screen and let the user pick on it.

    mode "point" returns (x, y) on click; mode "rect" returns
    (x1, y1, x2, y2) after a click-drag.  Coordinates are in page space
    (CSS pixels).  Esc or closing the window cancels and returns None.
    """
    import tkinter as tk
    from PIL import ImageTk

    root = tk.Tk()
    root.title(title)
    root.attributes("-topmost", True)
    root.resizable(False, False)

    scale = min(1.0,
                0.9 * root.winfo_screenwidth() / img.width,
                0.85 * root.winfo_screenheight() / img.height)
    if scale < 1.0:
        disp = img.resize((max(1, int(img.width * scale)),
                           max(1, int(img.height * scale))), Image.LANCZOS)
    else:
        disp = img

    instructions = ("Click the target spot.  Esc cancels."
                    if mode == "point" else
                    "Drag a rectangle around the region.  Esc cancels.")
    status = tk.Label(root, text=instructions, font=("Segoe UI", 11), anchor="w")
    status.pack(fill="x", padx=6, pady=3)

    photo = ImageTk.PhotoImage(disp)
    canvas = tk.Canvas(root, width=disp.width, height=disp.height,
                       highlightthickness=0, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")

    result = {}
    drag = {}

    def to_page(cx, cy):
        x = min(max(int(round(cx / scale)), 0), img.width - 1)
        y = min(max(int(round(cy / scale)), 0), img.height - 1)
        return x, y

    def on_motion(event):
        x, y = to_page(event.x, event.y)
        canvas.delete("cross")
        canvas.create_line(event.x, 0, event.x, disp.height,
                           fill="#ff3333", tags="cross")
        canvas.create_line(0, event.y, disp.width, event.y,
                           fill="#ff3333", tags="cross")
        status.config(text="%s   (x=%d, y=%d)" % (instructions, x, y))
        if mode == "rect" and "start" in drag:
            canvas.delete("band")
            canvas.create_rectangle(drag["cx"], drag["cy"], event.x, event.y,
                                    outline="#00ccff", width=2, tags="band")

    def on_press(event):
        if mode == "point":
            result["value"] = to_page(event.x, event.y)
            root.destroy()
        else:
            drag["start"] = to_page(event.x, event.y)
            drag["cx"], drag["cy"] = event.x, event.y

    def on_release(event):
        if mode != "rect" or "start" not in drag:
            return
        x1, y1 = drag.pop("start")
        x2, y2 = to_page(event.x, event.y)
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 3 or y2 - y1 < 3:
            status.config(text="Rectangle too small -- drag again.  Esc cancels.")
            canvas.delete("band")
            return
        result["value"] = (x1, y1, x2, y2)
        root.destroy()

    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Button-1>", on_press)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", lambda e: root.destroy())
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.lift()
    root.focus_force()
    root.mainloop()
    return result.get("value")


def ask_comment():
    return input("Optional comment (Enter for none): ").strip()


def do_click(writer, double):
    label = "Double click" if double else "Click"
    comment = ask_comment()
    print("Taking screenshot -- pick the %s position..." % label.lower())
    img = psl.viewportGrab()
    point = _run_picker(img, "%s -- pick the position" % label, "point")
    if point is None:
        print("Cancelled -- nothing recorded.")
        return
    x, y = point
    if double:
        psl.doubleClick(x, y)
        writer.action(comment, "Double click at (%d, %d)" % (x, y),
                      "psl.doubleClick(%d, %d)" % (x, y))
    else:
        psl.click(x, y)
        writer.action(comment, "Click at (%d, %d)" % (x, y),
                      "psl.click(%d, %d)" % (x, y))
    print("%s performed at (%d, %d) and recorded." % (label, x, y))


def do_sendkeys(writer):
    comment = ask_comment()
    print(r"Special keys: type \r where Enter should be pressed (only \r and \n are interpreted).")
    raw = input("Text to type: ")
    if not raw:
        print("Nothing to type -- cancelled.")
        return
    text = raw.replace("\\r", "\r").replace("\\n", "\n")
    psl.sendkeys(text)
    writer.action(comment, "Send keys %r" % text, "psl.sendkeys(%r)" % text)
    print("Keys sent and recorded.")


def do_screen_test(writer, script_base, script_dir):
    comment = ask_comment()
    while True:
        name = input("Name for this screen test (letters, digits, underscore): ").strip()
        if not name:
            continue
        if not name.isidentifier() or keyword.iskeyword(name) or name.startswith("_"):
            print("Please use letters/digits/underscore, not starting with a digit or underscore.")
            continue
        if list_captures(script_dir, script_base, name) and input(
                "Screen test '%s' already has a capture -- replace it? [y/N]: "
                % name).strip().lower() != "y":
            continue
        break
    print("Taking screenshot -- drag a rectangle around the area to test...")
    img = psl.viewportGrab()
    box = _run_picker(img, "Screen test '%s' -- drag the area" % name, "rect")
    if box is None:
        print("Cancelled -- nothing recorded.")
        return
    x1, y1, x2, y2 = box

    while True:
        s = input("matchLevel 0.0-1.0 [0.98]: ").strip() or "0.98"
        try:
            level = float(s)
        except ValueError:
            level = -1.0
        if 0.0 <= level <= 1.0:
            break
        print("Enter a number between 0.0 and 1.0.")
    message = input("Alarm message [Screen does not match %s]: " % name).strip() \
        or "Screen does not match %s" % name

    # the saved baseline is the exact crop of the screenshot the user picked on
    for old_fn, _, _, _ in list_captures(script_dir, script_base, name):
        os.remove(os.path.join(script_dir, old_fn))
    png_name = capture_filename(script_base, name, box)
    img.crop(box).save(os.path.join(script_dir, png_name))

    writer.action(comment,
                  "Screen test '%s' (matchLevel %s)" % (name, level),
                  "psl.verifyFrame(%r, (%d, %d, %d, %d), %s, %r)"
                  % (png_name, x1, y1, x2, y2, level, message))
    print("Screen test '%s' recorded; baseline saved to %s"
          % (name, os.path.join(script_dir, png_name)))


def do_wait(writer):
    comment = ask_comment()
    while True:
        s = input("Seconds to wait [1]: ").strip() or "1"
        try:
            seconds = float(s)
        except ValueError:
            seconds = 0.0
        if seconds > 0:
            break
        print("Enter a positive number.")
    if seconds == int(seconds):
        seconds = int(seconds)
    writer.action(comment, "Wait %s second(s)" % seconds, "psl.wait(%s)" % seconds)
    print("Wait recorded (not executed now).")


def main():
    print("=== playwrightscript recorder ===\n")

    while True:
        fname = input("Filename for the new script (Enter = immediate mode, "
                      "code prints to the console only): ").strip()
        if not fname:
            fname = None
            break
        if not fname.lower().endswith(".py"):
            fname += ".py"
        if os.path.exists(fname) and input(
                "%s exists -- overwrite? [y/N]: " % fname).strip().lower() != "y":
            continue
        break
    if fname is None:
        script_dir = os.getcwd()
        script_base = "immediate"
    else:
        script_path = os.path.abspath(fname)
        script_dir = os.path.dirname(script_path)
        script_base = os.path.splitext(os.path.basename(script_path))[0]

    log_display = ("%s.log" % script_base) if fname is not None else "<scriptname>.log"
    want_log = input("Log output to %s when the script runs? [Y/n]: "
                     % log_display).strip().lower() != "n"

    print()
    print(DEBUG_URL_HELP)
    while True:
        url = input("Debug URL [http://127.0.0.1:9222]: ").strip() or "http://127.0.0.1:9222"
        try:
            psl.connect(url)
            break
        except Exception as e:
            print("Could not connect: %s" % e)
            print("Is Chrome running with --remote-debugging-port?  Try again.\n")
    if url.startswith("ws"):
        print("Note: ws:// URLs change every Chrome restart. When replaying later you")
        print("can pass a fresh URL as the script's first argument.")

    pages = psl.listPages()
    if len(pages) > 1:
        print("\nOpen tabs:")
        for i, title, page_url in pages:
            print("  %d. %s  --  %s" % (i + 1, (title or "<untitled>")[:60], page_url[:90]))
        while True:
            s = input("Which tab should the script drive? [1]: ").strip() or "1"
            if s.isdigit() and 1 <= int(s) <= len(pages):
                index = int(s) - 1
                break
            print("Please choose 1-%d." % len(pages))
        psl.usePage(index)
        page_url = pages[index][2]
    else:
        page_url = pages[0][2]

    hint = urlparse(page_url).netloc or page_url
    vw, vh = psl.viewportSize()
    print("\nRecording against: %s  (viewport %dx%d)" % (page_url[:90], vw, vh))

    if fname is None:
        writer = ConsoleWriter()
        print("Immediate mode: each action runs right away and its code block "
              "prints below; nothing is saved.")
    else:
        writer = ScriptWriter(fname)
        print("Writing to %s (saved after every step)." % fname)
    header = [
        "# Recorded by playwrightscriptrecord.py on %s"
        % datetime.now().strftime("%Y-%m-%d %H:%M"),
        "import sys",
        "",
        "import playwrightscriptlib as psl",
        "",
    ]
    if want_log:
        header.append("psl.logging(True)")
    header += [
        "psl.alarmOnError()",
        "psl.info('Connecting to the browser')",
        "psl.connect(sys.argv[1] if len(sys.argv) > 1 else %r, page_hint=%r)"
        % (url, hint),
        "psl.checkViewport(%d, %d)" % (vw, vh),
    ]
    writer.header(header)

    while True:
        print(MENU)
        choice = input("> ").strip()
        try:
            if choice == "1":
                do_click(writer, double=False)
            elif choice == "2":
                do_click(writer, double=True)
            elif choice == "3":
                do_sendkeys(writer)
            elif choice == "4":
                do_screen_test(writer, script_base, script_dir)
            elif choice == "5":
                do_wait(writer)
            elif choice == "6":
                break
            else:
                print("Please choose 1-6.")
        except Exception as e:
            print("Action failed (nothing was recorded for it): %s" % e)

    writer.finish()
    psl.disconnect()
    if fname is None:
        print("\nDone (immediate mode -- nothing was saved).")
    else:
        print("\nDone. Script saved to %s" % os.path.abspath(fname))
        print("Replay with:  python %s  [debug-url]" % fname)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nRecording aborted. Anything already written to a script file is saved.")
