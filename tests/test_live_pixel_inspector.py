"""Pixel inspector refresh checks with fake widgets, without a display or browser."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlib as psl


class Widget:
    def __init__(self, parent=None, **options):
        self.children = []
        self.options = options
        self.bindings = {}
        self.items = []
        self.offsets = [0.0, 0.0]
        if parent is not None:
            parent.children.append(self)

    def pack(self, **kwargs):
        pass

    def grid(self, **kwargs):
        pass

    def title(self, value):
        self.window_title = value

    def attributes(self, *args):
        pass

    def config(self, **kwargs):
        self.options.update(kwargs)

    def bind(self, event, callback):
        self.bindings[event] = callback

    def delete(self, tag):
        if tag == "all":
            self.items.clear()
        else:
            self.items = [item for item in self.items if item[2].get("tags") != tag]

    def create_image(self, *args, **kwargs):
        self.items.append(("image", args, kwargs))

    def create_line(self, *args, **kwargs):
        self.items.append(("line", args, kwargs))

    def create_rectangle(self, *args, **kwargs):
        self.items.append(("rectangle", args, kwargs))

    def xview(self, *args):
        return self.offsets[0], 1.0

    def yview(self, *args):
        return self.offsets[1], 1.0

    def xview_moveto(self, value):
        self.offsets[0] = value

    def yview_moveto(self, value):
        self.offsets[1] = value

    def canvasx(self, value):
        return value + self.offsets[0] * self.options["scrollregion"][2]

    def canvasy(self, value):
        return value + self.offsets[1] * self.options["scrollregion"][3]

    def set(self, *args):
        pass

    def lift(self):
        pass

    def destroy(self):
        pass


class LivePixelInspectorTests(unittest.TestCase):
    def setUp(self):
        widgets = []

        def make_widget(*args, **kwargs):
            widget = Widget(*args, **kwargs)
            widgets.append(widget)
            return widget

        fake_tk = SimpleNamespace(**{name: make_widget for name in
                                    ("Toplevel", "Label", "Frame", "Canvas", "Scrollbar", "Button")})
        patcher = mock.patch.dict(sys.modules, {"tkinter": fake_tk})
        patcher.start()
        self.addCleanup(patcher.stop)
        photos = mock.patch.object(ImageTk, "PhotoImage", side_effect=lambda image, master: image.copy())
        photos.start()
        self.addCleanup(photos.stop)
        self.expected = Image.new("RGB", (20, 10), (10, 20, 30))
        self.top = psl._pixel_inspector(None, self.expected,
                                        Image.new("RGB", (20, 10), (20, 30, 40)))
        self.widgets = widgets
        self.canvases = [widget for widget in widgets if "scrollregion" in widget.options]
        self.info = self.top.children[0]

    def displayed_image(self, canvas):
        return next(item[2]["image"] for item in canvas.items if item[0] == "image")

    def test_refresh_preserves_zoom_scroll_selection_and_updates_rgb(self):
        exp, actual = self.canvases
        # The 20-by-10 capture starts at 24x zoom; select source pixel (2, 3).
        actual.bindings["<Button-1>"](SimpleNamespace(x=49, y=73))
        zoom_in = next(widget for widget in self.widgets if widget.options.get("text") == "Zoom in")
        zoom_in.options["command"]()
        for canvas in self.canvases:
            canvas.xview_moveto(0.25)
            canvas.yview_moveto(0.125)
        self.top.update_actual(Image.new("RGB", (20, 10), (15, 100, 35)))
        self.assertEqual(self.displayed_image(exp).size, (960, 480))
        self.assertEqual(self.displayed_image(exp).getpixel((0, 0)), (10, 20, 30))
        self.assertEqual(self.displayed_image(actual).getpixel((0, 0)), (15, 100, 35))
        for canvas in self.canvases:
            self.assertEqual(canvas.offsets, [0.25, 0.125])
            selection = [item for item in canvas.items if item[2].get("tags") == "sel"]
            self.assertEqual(len(selection), 1)
            self.assertEqual(selection[0][1], (96, 144, 144, 192))
        self.assertIn("Pixel (2, 3)", self.info.options["text"])
        self.assertIn("expected RGB (10, 20, 30)", self.info.options["text"])
        self.assertIn("actual RGB (15, 100, 35)", self.info.options["text"])
        self.assertIn("max channel diff 80", self.info.options["text"])

    def test_wrong_size_capture_is_rejected_without_replacing_pixels(self):
        _, actual = self.canvases
        with self.assertRaisesRegex(ValueError, "does not match expected size"):
            self.top.update_actual(Image.new("RGB", (21, 10), "red"))
        self.assertEqual(self.displayed_image(actual).getpixel((0, 0)), (20, 30, 40))

    def test_update_converts_pixels_to_rgb_and_keeps_caller_image_independent(self):
        incoming = Image.new("L", (20, 10), 77)
        self.top.update_actual(incoming)
        incoming.putpixel((0, 0), 99)
        actual = self.displayed_image(self.canvases[1])
        self.assertEqual(actual.mode, "RGB")
        self.assertEqual(actual.getpixel((0, 0)), (77, 77, 77))
        self.assertEqual(self.expected.getpixel((0, 0)), (10, 20, 30))

    def test_live_status_can_report_pause_and_capture_failure(self):
        status = self.top.children[1]
        self.top.set_live_status("Paused")
        self.assertEqual(status.options["text"], "Paused")
        self.top.set_live_status("Capture failed: browser disconnected")
        self.assertEqual(status.options["text"], "Capture failed: browser disconnected")

    def test_large_capture_zoom_is_bounded_for_repeated_live_updates(self):
        start = len(self.widgets)
        large = Image.new("RGB", (500, 500), "white")
        top = psl._pixel_inspector(None, large, large)
        widgets = self.widgets[start:]
        zoom_in = next(widget for widget in widgets if widget.options.get("text") == "Zoom in")
        for _ in range(7):
            zoom_in.options["command"]()
        top.update_actual(large)
        canvases = [widget for widget in widgets if "scrollregion" in widget.options]
        self.assertEqual(len(canvases), 2)
        for canvas in canvases:
            self.assertEqual(self.displayed_image(canvas).size, (2000, 2000))
        self.assertTrue(any(widget.options.get("text") == "zoom 4x" for widget in widgets))


if __name__ == "__main__":
    unittest.main()
