"""Integration checks for the live comparison event pump using only fake Tk."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import playwrightscriptlib as psl
from test_live_pixel_inspector import Widget


class Variable:
    def __init__(self, master=None, value=False):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class EventWidget(Widget):
    def __init__(self, harness, kind, parent=None, **options):
        super().__init__(parent, **options)
        self.harness = harness
        self.kind = kind
        self.exists = True
        self.protocols = {}
        self.destroy_count = 0

    def winfo_exists(self):
        return self.exists

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def winfo_width(self):
        return 600

    def winfo_height(self):
        return 400

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def update_idletasks(self):
        pass

    def geometry(self, value):
        self.geometry_value = value

    def focus_force(self):
        pass

    def create_image(self, *args, **kwargs):
        super().create_image(*args, **kwargs)
        return len(self.items)

    def create_rectangle(self, *args, **kwargs):
        super().create_rectangle(*args, **kwargs)
        return len(self.items)

    def itemconfigure(self, item_id, **options):
        self.items[item_id - 1][2].update(options)

    def coords(self, item_id, *values):
        kind, _, options = self.items[item_id - 1]
        self.items[item_id - 1] = (kind, values, options)

    def update(self):
        h = self.harness
        h.ticks += 1
        if h.ticks > 12:
            raise AssertionError("Fake Tk event loop exceeded its bounded tick budget")
        h.in_callback = True
        try:
            if h.events:
                h.events.pop(0)()
            else:
                raise AssertionError("Viewer continued after all planned UI events")
        finally:
            h.in_callback = False

    def destroy(self):
        self.destroy_count += 1
        self.exists = False
        for child in self.children:
            child.destroy()


class Harness:
    def __init__(self):
        self.widgets = []
        self.roots = []
        self.events = []
        self.in_callback = False
        self.ticks = 0
        self.now = 0.0
        self.controllers = []
        self.tk = SimpleNamespace(BooleanVar=Variable, TclError=type("TclError", (Exception,), {}))
        for kind in ("Tk", "Toplevel", "Frame", "Label", "Canvas", "Scrollbar", "Button", "Checkbutton"):
            setattr(self.tk, kind, self.factory(kind))

    def factory(self, kind):
        def make(parent=None, **options):
            widget = EventWidget(self, kind, parent, **options)
            self.widgets.append(widget)
            if kind == "Tk":
                self.roots.append(widget)
            return widget
        return make

    def widget(self, text):
        return next(widget for widget in self.widgets if widget.options.get("text") == text)

    def click(self, text):
        widget = self.widget(text)
        if widget.kind == "Checkbutton":
            var = widget.options["variable"]
            var.set(not var.get())
        widget.options["command"]()

    def titled(self, title):
        return next(widget for widget in self.widgets if getattr(widget, "window_title", None) == title)

    def sleep(self, duration):
        self.now += duration


class LiveDiffViewerTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.expected = Image.new("RGB", (20, 10), "black")
        self.initial = Image.new("RGB", (20, 10), "white")
        self.box = (10, 5, 30, 15)
        self.initial_full = self.full_frame(self.initial, 5)
        controller_class = psl._LiveCompareRefresh

        def controller_factory(*args, **kwargs):
            controller = controller_class(*args, **kwargs)
            self.h.controllers.append(controller)
            return controller

        for patcher in (
            mock.patch.dict(sys.modules, {"tkinter": self.h.tk}),
            mock.patch.object(ImageTk, "PhotoImage", side_effect=lambda image, master: image.copy()),
            mock.patch.object(psl, "_LiveCompareRefresh", side_effect=controller_factory),
            mock.patch.object(psl.time, "monotonic", side_effect=lambda: self.h.now),
            mock.patch.object(psl.time, "sleep", side_effect=self.h.sleep),
            mock.patch.object(psl, "viewportGrab", side_effect=AssertionError("Unexpected browser screenshot")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def full_frame(self, crop, marker):
        full = Image.new("RGB", (60, 40), (marker, marker, marker))
        full.paste(crop, self.box[:2])
        return full

    def run_viewer(self, capture):
        return psl._diff_viewer(self.expected, self.initial, 0.0, 0.98,
                                self.box, self.initial_full, refresh_callback=capture)

    def assert_cleaned_up(self):
        self.assertEqual(len(self.h.roots), 1)
        self.assertEqual(self.h.roots[0].destroy_count, 1)
        self.assertTrue(all(not widget.exists for widget in self.h.widgets))
        self.assertEqual(len(self.h.controllers), 1)
        self.assertTrue(self.h.controllers[0].closed)
        self.assertFalse(self.h.controllers[0].pending)

    def current_panes(self):
        root = self.h.roots[0]
        row = next(child for child in root.children if child.kind == "Frame")
        return [pane.children[1] for pane in row.children]

    def test_shared_frames_refresh_pixels_context_score_and_pause_controls(self):
        h = self.h
        first = Image.new("RGB", (20, 10), (90, 40, 20))
        second = self.expected.copy()
        samples = [(first, self.full_frame(first, 11)), (second, self.full_frame(second, 22))]
        capture_count = []

        def capture():
            self.assertFalse(h.in_callback, "Screenshot must happen outside Tk event callbacks")
            capture_count.append(1)
            return samples.pop(0)

        def open_views():
            h.click("View pixels")
            h.click("Show current capture inside full frame grab")
            pixels = h.titled("Pixel inspector")
            canvas = next(child for child in pixels.children if child.kind == "Frame").children[3]
            canvas.bindings["<Button-1>"](SimpleNamespace(x=49, y=73))
            h.click("Highlight differences (red)")

        def assert_frame(crop_color, marker, score_text):
            exp, actual = self.current_panes()
            self.assertEqual(exp.options["image"].getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(actual.options["image"].getpixel((0, 0)), crop_color)
            pixels = h.titled("Pixel inspector")
            self.assertIn("Pixel (2, 3)", pixels.children[0].options["text"])
            self.assertIn("actual RGB %s" % (crop_color,), pixels.children[0].options["text"])
            context = h.titled("Live capture region in full frame")
            self.assertEqual(context._photo.getpixel((0, 0)), (marker, marker, marker))
            self.assertEqual(context._photo.getpixel(self.box[:2]), crop_color)
            score_label = h.roots[0].children[1]
            self.assertTrue(score_label.options["text"].startswith(score_text))

        def after_first_capture():
            assert_frame((90, 40, 20), 11, "MISMATCH")
            self.assertIn("max channel diff 90", h.titled("Pixel inspector").children[0].options["text"])
            h.click("Highlight differences (red)")
            expected_pane, actual_pane = self.current_panes()
            tinted = expected_pane.options["image"].getpixel((0, 0))
            self.assertGreater(tinted[0], tinted[1])
            self.assertNotEqual(actual_pane.options["image"].getpixel((0, 0)), (90, 40, 20))
            h.click("Pause live refresh")
            self.assertEqual(len(capture_count), 1)

        def while_paused():
            self.assertEqual(len(capture_count), 1)
            h.click("Refresh now")

        def after_manual_capture():
            # The highlight toggle remains enabled; this fresh matching frame
            # must clear the previous red difference overlay from both panes.
            assert_frame((0, 0, 0), 22, "MATCH")
            self.assertIn("max channel diff 0", h.titled("Pixel inspector").children[0].options["text"])
            self.assertEqual(len(capture_count), 2)
            self.assertTrue(h.roots[0].children[2].options["text"].startswith("Paused"))
            # Reopening reuses both windows; it must not capture independently.
            h.click("View pixels")
            h.click("Show current capture inside full frame grab")
            self.assertEqual(sum(widget.kind == "Toplevel" for widget in h.widgets), 2)
            h.click("Back to alarm")

        h.events = [open_views, after_first_capture, while_paused, after_manual_capture]
        self.run_viewer(capture)
        self.assertEqual(len(capture_count), 2)
        self.assert_cleaned_up()

    def test_capture_error_removes_green_match_and_marks_retained_images_stale(self):
        h = self.h
        capture = mock.Mock(side_effect=[(self.expected, self.full_frame(self.expected, 22)),
                                         RuntimeError("browser disconnected")])

        def open_views():
            h.click("View pixels")
            h.click("Show current capture inside full frame grab")

        def request_failure():
            self.assertEqual(h.roots[0].children[1].options["fg"], "#176b35")
            h.click("Refresh now")

        def inspect_failure():
            label = h.roots[0].children[1]
            self.assertEqual(label.options["fg"], "#b00020")
            self.assertIn("No current comparison", label.options["text"])
            self.assertIn("browser disconnected", h.roots[0].children[2].options["text"])
            pixels = h.titled("Pixel inspector")
            context = h.titled("Live capture region in full frame")
            self.assertIn("last captured image", pixels.children[1].options["text"])
            self.assertIn("browser disconnected", context.children[1].options["text"])
            self.assertEqual(context._photo.getpixel((0, 0)), (22, 22, 22))
            h.click("Back to alarm")

        h.events = [open_views, request_failure, inspect_failure]
        self.run_viewer(capture)
        self.assertEqual(capture.call_count, 2)
        self.assert_cleaned_up()

    def test_window_close_escape_and_back_stop_before_capture(self):
        for close_method in ("x", "escape", "back"):
            with self.subTest(close_method=close_method):
                self.h.widgets.clear()
                self.h.roots.clear()
                self.h.controllers.clear()
                self.h.ticks = 0

                def close():
                    root = self.h.roots[0]
                    if close_method == "x":
                        root.protocols["WM_DELETE_WINDOW"]()
                    elif close_method == "escape":
                        root.bindings["<Escape>"](None)
                    else:
                        self.h.click("Back to alarm")

                self.h.events = [close]
                capture = mock.Mock(side_effect=AssertionError("Capture after closure"))
                self.run_viewer(capture)
                capture.assert_not_called()
                self.assert_cleaned_up()

    def test_keyboard_interrupt_destroys_windows_and_closes_refresh_controller(self):
        self.h.events = [lambda: self.h.click("View pixels")]
        with self.assertRaises(KeyboardInterrupt):
            self.run_viewer(mock.Mock(side_effect=KeyboardInterrupt()))
        self.assert_cleaned_up()

    def test_render_failure_destroys_windows_and_closes_refresh_controller(self):
        self.h.events = [lambda: self.h.click("View pixels")]
        calls = []

        def photo(image, master):
            calls.append(master)
            # Initial panes + initial inspector require four photos; fail
            # during rendering of the first newly captured comparison.
            if len(calls) == 5:
                raise RuntimeError("PhotoImage rendering failed")
            return image.copy()

        with mock.patch.object(ImageTk, "PhotoImage", side_effect=photo):
            with self.assertRaisesRegex(RuntimeError, "rendering failed"):
                self.run_viewer(lambda: (self.expected, self.initial_full))
        self.assert_cleaned_up()


if __name__ == "__main__":
    unittest.main()
