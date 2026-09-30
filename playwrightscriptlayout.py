"""Persistent named screen layouts and operator-driven definition dialogs.

This module never clicks the controlled application.  Selection happens on a
frozen screenshot; the caller performs its action after resolution succeeds.
All GUI imports and dialogs are deferred until explicitly requested.
"""

import json
import os
from pathlib import Path
import re
import tempfile
import uuid

from PIL import Image


class LayoutError(Exception):
    """A layout is invalid or cannot safely be used on this viewport."""


class ViewportMismatchError(LayoutError):
    """Valid browser dimensions differ from those saved in the layout."""


class LayoutCancelled(SystemExit):
    """The operator stopped layout selection/definition; stop the script."""

    def __init__(self, code=2):
        super().__init__(code)


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
_RESERVED.update("COM%d" % n for n in range(10))
_RESERVED.update("LPT%d" % n for n in range(10))


def validate_name(name):
    """Return a safe, portable entry name or raise LayoutError."""
    if (not isinstance(name, str) or not _NAME_RE.fullmatch(name)
            or name.upper() in _RESERVED or name.lower() == "layout"):
        raise LayoutError(
            "Invalid layout name %r. Use 1-100 letters, digits, underscores "
            "or hyphens, starting with a letter or digit; 'layout' and "
            "Windows device names are reserved." % (name,))
    return name


def _integers(value, count):
    return (isinstance(value, (list, tuple)) and len(value) == count
            and all(type(n) is int for n in value))


def _size(value):
    if not _integers(value, 2) or min(value) <= 0:
        raise LayoutError("Viewport must contain two positive integer dimensions.")
    return tuple(value)


def _read_json(path):
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError) as exc:
        raise LayoutError("Cannot read layout file %s: %s" % (path, exc)) from exc
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise LayoutError("Unsupported or missing version in layout file %s." % path)
    return value


def _write_json(path, value):
    """Commit a complete JSON file without exposing a partial write."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", dir=str(path.parent),
                prefix=".layout-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    except OSError as exc:
        raise LayoutError("Cannot save layout file %s: %s" % (path, exc)) from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class Layout:
    """A folder with layout.json, one JSON per name, and reference PNGs."""

    def __init__(self, path):
        try:
            self.path = Path(path).expanduser().resolve()
            self.path.mkdir(parents=True, exist_ok=True)
        except (OSError, TypeError, ValueError) as exc:
            raise LayoutError("Cannot open layout folder %r: %s" % (path, exc)) from exc
        self.viewport = None
        metadata = self.path / "layout.json"
        if metadata.exists():
            value = _read_json(metadata)
            if "viewport" not in value:
                raise LayoutError("Missing viewport in %s." % metadata)
            self.viewport = _size(value["viewport"])
        elif any(self.path.glob("*.json")):
            raise LayoutError(
                "Layout folder %s contains entry JSON files but layout.json is missing. "
                "Restore its original metadata or choose a new empty layout folder."
                % self.path)

    def check_viewport(self, size):
        """Bind a new layout once, then require an exact viewport size match."""
        actual = _size(size)
        if self.viewport is None:
            _write_json(self.path / "layout.json", {"version": 1, "viewport": list(actual)})
            self.viewport = actual
        elif self.viewport != actual:
            raise ViewportMismatchError(
                "Layout %s expects viewport %dx%d, but the current viewport is "
                "%dx%d. Restore its size or choose a layout for this screen." %
                ((self.path,) + self.viewport + actual))
        return actual

    def _entry_path(self, name):
        return self.path / (validate_name(name) + ".json")

    def _validate(self, name, kind, entry):
        if kind not in ("point", "region", "frame"):
            raise LayoutError("Unknown layout entry kind %r." % kind)
        if entry.get("kind") != kind:
            raise LayoutError("Layout entry %r is a %r, but this action requires a %s."
                              % (name, entry.get("kind"), kind))
        if kind == "point":
            point = (entry.get("x"), entry.get("y"))
            if not _integers(point, 2) or min(point) < 0:
                raise LayoutError("Invalid point coordinates for %r." % name)
            if self.viewport and (point[0] >= self.viewport[0] or point[1] >= self.viewport[1]):
                raise LayoutError("Point %r is outside the layout viewport." % name)
        else:
            box = entry.get("box")
            if (not _integers(box, 4) or min(box) < 0
                    or box[0] >= box[2] or box[1] >= box[3]):
                raise LayoutError("Invalid rectangle bounds for %r." % name)
            if self.viewport and (box[2] > self.viewport[0] or box[3] > self.viewport[1]):
                raise LayoutError("Rectangle %r is outside the layout viewport." % name)
            if kind == "frame":
                filename = entry.get("image")
                if not isinstance(filename, str) or not filename:
                    raise LayoutError("Missing reference image for %r." % name)
                relative = Path(filename)
                if relative.is_absolute() or relative.drive:
                    raise LayoutError("Reference image for %r must be relative to the layout folder." % name)
                try:
                    target = (self.path / relative).resolve()
                    target.relative_to(self.path)
                except (ValueError, OSError) as exc:
                    raise LayoutError("Reference image for %r escapes the layout folder." % name) from exc
                try:
                    with Image.open(target) as reference:
                        if reference.format != "PNG":
                            raise LayoutError("Reference image for %r must be a PNG." % name)
                        if reference.size != (box[2] - box[0], box[3] - box[1]):
                            raise LayoutError("Reference image dimensions do not match rectangle %r." % name)
                        reference.load()
                except (OSError, ValueError) as exc:
                    raise LayoutError("Cannot read reference image for %r: %s" % (name, exc)) from exc
                entry = dict(entry, image=str(target))
        return entry

    def resolve(self, name, kind, grab):
        """Load a valid entry, or ask the operator to define a missing entry."""
        path = self._entry_path(name)
        if kind not in ("point", "region", "frame"):
            raise LayoutError("Unknown layout entry kind %r." % kind)
        if path.exists():
            return self._validate(name, kind, _read_json(path))
        return self.define(name, kind, grab)

    def define(self, name, kind, grab):
        """Define/redefine an entry; cancellation leaves its previous files intact."""
        path = self._entry_path(name)
        if kind not in ("point", "region", "frame"):
            raise LayoutError("Unknown layout entry kind %r." % kind)
        if not prompt_definition(name, kind):
            raise LayoutCancelled()
        screenshot = grab()
        if not isinstance(screenshot, Image.Image):
            raise LayoutError("The screenshot provider must return a PIL image.")
        self.check_viewport(screenshot.size)
        selection = pick_on_image(
            screenshot, "Define %s '%s'" % (kind, name),
            "point" if kind == "point" else "rect")
        if selection is None:
            raise LayoutCancelled()
        entry = {"version": 1, "kind": kind}
        if kind == "point":
            if not _integers(selection, 2):
                raise LayoutError("The point picker returned invalid coordinates.")
            entry.update(x=selection[0], y=selection[1])
        else:
            entry["box"] = list(selection)
        # Validate the picked geometry before cropping (PIL pads outside bounds).
        self._validate(name, "point" if kind == "point" else "region",
                       dict(entry, kind="point" if kind == "point" else "region"))
        image_path = None
        if kind == "frame":
            crop = screenshot.crop(tuple(entry["box"]))
            if not confirm_reference(name, crop):
                raise LayoutCancelled()
            filename = "%s-%s.png" % (name, uuid.uuid4().hex)
            image_path = self.path / filename
            temporary = self.path / (".reference-%s.tmp" % uuid.uuid4().hex)
            try:
                crop.save(temporary, format="PNG")
                os.replace(str(temporary), str(image_path))
            except OSError as exc:
                raise LayoutError("Cannot save reference image for %r: %s" % (name, exc)) from exc
            finally:
                if temporary.exists():
                    temporary.unlink()
            entry["image"] = filename
        try:
            resolved = self._validate(name, kind, entry)
            _write_json(path, entry)
        except BaseException:
            if image_path is not None and image_path.exists():
                image_path.unlink()
            raise
        return resolved


def _root(title):
    import tkinter as tk
    root = None
    try:
        parent = getattr(tk, "_default_root", None)
        if parent is not None and parent.winfo_exists():
            # Share the operator window's interpreter so the local grab also
            # blocks its Stop/close controls while this definition is pending.
            # A second Tk would leave those controls active in another app,
            # letting cancellation race with the action after definition.
            root = tk.Toplevel(parent)
            root.transient(parent)
            if os.name == "nt":
                # A local grab does not control Windows title-bar Close. Keep
                # this owner disabled until the child ends so its cancellation
                # handler cannot run unnoticed behind the pending definition.
                was_disabled = parent.attributes("-disabled")

                def restore_parent(event):
                    if event.widget is root:
                        try:
                            parent.attributes("-disabled", was_disabled)
                        except tk.TclError:
                            pass  # the owner itself may already be gone

                root.bind("<Destroy>", restore_parent, add="+")
                parent.attributes("-disabled", True)
            root.grab_set()
        else:
            root = tk.Tk()
        root.title(title)
        root.attributes("-topmost", True)
        return root
    except tk.TclError as exc:
        if root is not None:
            root.destroy()
        raise LayoutError("Cannot open layout dialog: %s" % exc) from exc


def choose_layout_folder():
    """Browse for a layout folder; the operator may create a new folder."""
    from tkinter import filedialog
    root = _root("Choose screen layout")
    root.withdraw()
    try:
        chosen = filedialog.askdirectory(
            parent=root, title="Choose or create a screen layout folder", mustexist=False)
    finally:
        root.destroy()
    if not chosen:
        raise LayoutCancelled()
    return chosen


def prompt_definition(name, kind):
    """Ask before taking the frozen screenshot used to define an entry."""
    import tkinter as tk
    root = _root("Define layout item")
    result = {"accepted": False}
    if kind == "frame":
        message = (
            "Define the expected reference for '%s'.\n\n"
            "Wait for or establish the CORRECT EXPECTED STATE in the application "
            "before continuing. A busy or unfinished state must not be saved as ready.\n\n"
            "Choose Define reference to capture the current screen, select its "
            "rectangle, then explicitly accept the preview as the expected reference."
        ) % name
        button_text = "Define reference"
    elif kind == "point":
        message = ("Define the click location for '%s'.\n\n"
                   "Choose Define location, then click the target on a frozen screenshot. "
                   "The selection is saved before the script performs its requested action.") % name
        button_text = "Define location"
    else:
        message = ("Define the capture rectangle for '%s'.\n\n"
                   "Choose Define rectangle, then drag around the area on a frozen screenshot.") % name
        button_text = "Define rectangle"
    tk.Label(root, text=message, justify="left", wraplength=570,
             font=("Segoe UI", 11)).pack(padx=20, pady=20)
    buttons = tk.Frame(root)
    buttons.pack(padx=20, pady=(0, 20), anchor="e")

    def accept():
        result["accepted"] = True
        root.destroy()

    tk.Button(buttons, text="Stop script", command=root.destroy).pack(side="left", padx=6)
    tk.Button(buttons, text=button_text, command=accept).pack(side="left", padx=6)
    root.bind("<Escape>", lambda event: root.destroy())
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.lift()
    root.wait_window(root)
    return result["accepted"]


def confirm_reference(name, crop):
    """Show the exact crop at 1:1 and require explicit reference acceptance."""
    import tkinter as tk
    from PIL import ImageTk
    root = _root("Accept expected reference")
    result = {"accepted": False}
    tk.Label(root, text=("Expected reference for '%s' (%d x %d pixels)\n"
                        "Confirm this image shows the correct expected state. "
                        "Preview is 1:1; scroll if needed.") %
             (name, crop.width, crop.height), justify="left", wraplength=620,
             font=("Segoe UI", 11)).pack(padx=15, pady=12)
    frame = tk.Frame(root)
    frame.pack(fill="both", expand=True, padx=15)
    canvas = tk.Canvas(frame, width=min(crop.width, int(root.winfo_screenwidth() * 0.8)),
                       height=min(crop.height, int(root.winfo_screenheight() * 0.6)),
                       highlightthickness=1, scrollregion=(0, 0, crop.width, crop.height))
    horizontal = tk.Scrollbar(frame, orient="horizontal", command=canvas.xview)
    vertical = tk.Scrollbar(frame, orient="vertical", command=canvas.yview)
    canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    horizontal.grid(row=1, column=0, sticky="ew")
    vertical.grid(row=0, column=1, sticky="ns")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    photo = ImageTk.PhotoImage(crop, master=root)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    buttons = tk.Frame(root)
    buttons.pack(padx=15, pady=15, anchor="e")

    def accept():
        result["accepted"] = True
        root.destroy()

    tk.Button(buttons, text="Stop without saving", command=root.destroy).pack(side="left", padx=6)
    tk.Button(buttons, text="Use as expected reference", command=accept).pack(side="left", padx=6)
    root.bind("<Escape>", lambda event: root.destroy())
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.lift()
    root.wait_window(root)
    return result["accepted"]


def pick_on_image(img, title, mode):
    """Select a point or rectangle on a frozen image, using recorder gestures.

    The screenshot is fitted to the screen. Coordinates refer to original
    screenshot pixels; rectangle right/bottom bounds are exclusive for PIL.
    Escape or closing the window returns None without saving anything.
    """
    import tkinter as tk
    from PIL import ImageTk
    if mode not in ("point", "rect"):
        raise LayoutError("Unknown screenshot picker mode %r." % mode)
    if not isinstance(img, Image.Image) or min(img.size) <= 0:
        raise LayoutError("The screenshot picker requires a nonempty PIL image.")
    root = _root(title)
    root.resizable(False, False)
    scale = min(1.0, 0.9 * root.winfo_screenwidth() / img.width,
                0.80 * root.winfo_screenheight() / img.height)
    display_size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    display = img.resize(display_size, Image.LANCZOS) if scale < 1.0 else img
    scale_x, scale_y = display.width / img.width, display.height / img.height
    instructions = ("Click the target spot. Esc cancels." if mode == "point" else
                    "Drag a rectangle around the region. Esc cancels.")
    status = tk.Label(root, text=instructions, font=("Segoe UI", 11), anchor="w")
    status.pack(fill="x", padx=6, pady=3)
    photo = ImageTk.PhotoImage(display, master=root)
    canvas = tk.Canvas(root, width=display.width, height=display.height,
                       highlightthickness=0, cursor="crosshair")
    canvas.pack()
    canvas.create_image(0, 0, image=photo, anchor="nw")
    result, drag = {}, {}

    def to_image(cx, cy):
        max_x, max_y = img.width, img.height
        if mode == "point":
            max_x -= 1
            max_y -= 1
        return (min(max(int(round(cx / scale_x)), 0), max_x),
                min(max(int(round(cy / scale_y)), 0), max_y))

    def on_motion(event):
        x, y = to_image(event.x, event.y)
        canvas.delete("cross")
        canvas.create_line(event.x, 0, event.x, display.height, fill="#ff3333", tags="cross")
        canvas.create_line(0, event.y, display.width, event.y, fill="#ff3333", tags="cross")
        status.config(text="%s   (x=%d, y=%d)" % (instructions, x, y))
        if mode == "rect" and "start" in drag:
            canvas.delete("band")
            canvas.create_rectangle(drag["cx"], drag["cy"], event.x, event.y,
                                    outline="#00ccff", width=2, tags="band")

    def on_press(event):
        if mode == "point":
            result["value"] = to_image(event.x, event.y)
            root.destroy()
        else:
            drag["start"] = to_image(event.x, event.y)
            drag["cx"], drag["cy"] = event.x, event.y

    def on_release(event):
        if mode != "rect" or "start" not in drag:
            return
        x1, y1 = drag.pop("start")
        x2, y2 = to_image(event.x, event.y)
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 3 or y2 - y1 < 3:
            status.config(text="Rectangle too small -- drag again. Esc cancels.")
            canvas.delete("band")
            return
        result["value"] = (x1, y1, x2, y2)
        root.destroy()

    canvas.bind("<Motion>", on_motion)
    canvas.bind("<Button-1>", on_press)
    canvas.bind("<ButtonRelease-1>", on_release)
    root.bind("<Escape>", lambda event: root.destroy())
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.lift()
    root.focus_force()
    root.wait_window(root)
    return result.get("value")
