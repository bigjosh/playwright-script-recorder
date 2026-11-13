"""Recapture dialog checks without opening Tk, a browser, or the machine UI."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image, ImageTk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlayout as layouts


class FakeTclError(Exception):
    pass


class Widget:
    def __init__(self, harness, kind, parent=None, **options):
        self.harness, self.kind, self.options = harness, kind, options
        self.children, self.items = [], []
        self.bindings, self.protocols = {}, {}
        self.destroy_count = 0
        self.destroyed = False
        if parent is not None:
            parent.children.append(self)
        harness.widgets.append(self)

    def pack(self, **options):
        pass

    grid = pack
    rowconfigure = lambda self, *args, **options: None
    columnconfigure = rowconfigure
    resizable = rowconfigure
    lift = rowconfigure
    focus_force = rowconfigure

    def configure(self, **options):
        self.options.update(options)

    config = configure

    def winfo_screenwidth(self):
        return 1200

    def winfo_screenheight(self):
        return 900

    def bind(self, event, callback):
        self.bindings[event] = callback

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def create_image(self, *coords, **options):
        self.items.append(("image", coords, options))

    def create_line(self, *coords, **options):
        self.items.append(("line", coords, options))

    def create_rectangle(self, *coords, **options):
        self.items.append(("rectangle", coords, options))

    def delete(self, tag):
        self.items = [item for item in self.items if item[2].get("tags") != tag]

    def xview(self, *args):
        return (0, 1)

    yview = xview
    set = rowconfigure

    def wait_window(self, window):
        self.harness.action(self)
        if not self.destroyed:
            raise AssertionError("Dialog did not close after the planned action")

    def destroy(self):
        self.destroy_count += 1
        if self.destroyed:
            # Tk may report a destroyed interpreter when finally tries cleanup.
            raise FakeTclError("application has been destroyed")
        self.destroyed = True
        for child in self.children:
            child.destroy()


class Harness:
    def __init__(self):
        self.widgets, self.roots = [], []
        self.action = lambda root: root.destroy()
        self.tk = SimpleNamespace(TclError=FakeTclError)
        for kind in ("Frame", "Label", "Canvas", "Scrollbar", "Button"):
            setattr(self.tk, kind, self.factory(kind))

    def factory(self, kind):
        return lambda parent=None, **options: Widget(self, kind, parent, **options)

    def root(self, title):
        root = Widget(self, "Root", text=title)
        self.roots.append(root)
        return root

    def click(self, text):
        button = next(widget for widget in self.widgets
                      if widget.kind == "Button" and widget.options["text"] == text)
        button.options["command"]()

    def image_canvases(self):
        return [widget for widget in self.widgets
                if widget.kind == "Canvas" and any(item[0] == "image" for item in widget.items)]

    def images(self):
        return [next(item[2]["image"] for item in canvas.items if item[0] == "image")
                for canvas in self.image_canvases()]


class RecaptureUITests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.old = Image.new("RGB", (1400, 700), (12, 34, 56))
        self.new = Image.new("RGB", (120, 40), (65, 43, 21))
        self.patch(mock.patch.dict(sys.modules, {"tkinter": self.h.tk}))
        self.patch(mock.patch.object(layouts, "_root", side_effect=self.h.root))
        self.photos = self.patch(mock.patch.object(
            ImageTk, "PhotoImage", side_effect=lambda image, master: image.copy()))

    def patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def assert_cleanup(self):
        self.assertTrue(self.h.roots)
        self.assertTrue(all(widget.destroyed for widget in self.h.widgets))

    def assert_same_pixels(self, actual, expected):
        self.assertEqual(actual.size, expected.size)
        self.assertEqual(actual.tobytes(), expected.tobytes())

    def test_prompt_displays_original_pixels_at_one_to_one_until_explicit_accept(self):
        def accept(root):
            images = self.h.images()
            self.assertEqual(len(images), 1)
            self.assert_same_pixels(images[0], self.old)
            canvas = self.h.image_canvases()[0]
            self.assertLess(canvas.options["width"], self.old.width)
            self.assertEqual(canvas.options["scrollregion"], (0, 0, *self.old.size))
            self.h.click("Capture replacement")

        self.h.action = accept
        self.assertTrue(layouts.prompt_recapture("focus-ready", self.old))
        self.assert_cleanup()

    def test_prompt_cancel_escape_and_window_close_return_false(self):
        actions = (
            lambda root: self.h.click("Cancel recapture"),
            lambda root: root.bindings["<Escape>"](None),
            lambda root: root.protocols["WM_DELETE_WINDOW"](),
        )
        for action in actions:
            with self.subTest(action=action):
                self.h.widgets.clear()
                self.h.roots.clear()
                self.h.action = action
                self.assertFalse(layouts.prompt_recapture("focus-ready", self.old))
                self.assert_cleanup()

    def test_legacy_prompt_explains_fixed_rectangle(self):
        self.h.action = lambda root: self.h.click("Cancel recapture")
        self.assertFalse(layouts.prompt_recapture("legacy.png", self.old,
                                                 fixed_box=(10, 20, 50, 60)))
        text = "\n".join(widget.options.get("text", "") for widget in self.h.widgets)
        self.assertIn("same rectangle (10, 20, 50, 60)", text)
        self.assertNotIn("select its rectangle", text)

    def test_confirmation_displays_old_and_new_unscaled_before_accept(self):
        def accept(root):
            old, new = self.h.images()
            self.assert_same_pixels(old, self.old)
            self.assert_same_pixels(new, self.new)
            self.h.click("Use as expected reference")

        self.h.action = accept
        self.assertTrue(layouts.confirm_reference("focus-ready", self.new, previous=self.old))
        self.assert_cleanup()

    def test_confirmation_cancel_escape_and_window_close_do_not_accept(self):
        actions = (
            lambda root: self.h.click("Cancel recapture"),
            lambda root: root.bindings["<Escape>"](None),
            lambda root: root.protocols["WM_DELETE_WINDOW"](),
        )
        for action in actions:
            with self.subTest(action=action):
                self.h.widgets.clear()
                self.h.roots.clear()
                self.h.action = action
                self.assertFalse(layouts.confirm_reference("focus-ready", self.new, previous=self.old))
                self.assert_cleanup()

    def test_image_render_failure_closes_dialog_and_preserves_exception(self):
        for dialog in (lambda: layouts.prompt_recapture("focus-ready", self.old),
                       lambda: layouts.confirm_reference("focus-ready", self.new, previous=self.old)):
            with self.subTest(dialog=dialog):
                self.h.widgets.clear()
                self.h.roots.clear()
                self.photos.side_effect = RuntimeError("cannot render reference")
                with self.assertRaisesRegex(RuntimeError, "cannot render reference"):
                    dialog()
                self.assert_cleanup()

    def test_picker_keeps_old_reference_visible_and_maps_to_original_screenshot(self):
        screenshot = Image.new("RGB", (2400, 1800), "white")

        def select(root):
            self.assert_same_pixels(self.h.images()[1], self.old)
            canvas = next(widget for widget in self.h.widgets if "<Button-1>" in widget.bindings)
            # The screenshot is 720x540 on this display; a .3 scale in both axes.
            canvas.bindings["<Button-1>"](SimpleNamespace(x=180, y=135))
            canvas.bindings["<ButtonRelease-1>"](SimpleNamespace(x=360, y=270))

        self.h.action = select
        self.assertEqual(layouts.pick_on_image(screenshot, "Capture", "rect", reference=self.old),
                         (600, 450, 1200, 900))
        self.assert_cleanup()

    def test_picker_escape_and_window_close_cancel_selection(self):
        for action in (lambda root: root.bindings["<Escape>"](None),
                       lambda root: root.protocols["WM_DELETE_WINDOW"]()):
            with self.subTest(action=action):
                self.h.widgets.clear()
                self.h.roots.clear()
                self.h.action = action
                self.assertIsNone(layouts.pick_on_image(self.new, "Capture", "rect", reference=self.old))
                self.assert_cleanup()

    def test_picker_reference_render_failure_closes_partly_built_window(self):
        calls = []

        def render(image, master):
            calls.append(image)
            if len(calls) == 2:
                raise RuntimeError("cannot render old reference")
            return image.copy()

        self.photos.side_effect = render
        with self.assertRaisesRegex(RuntimeError, "cannot render old reference"):
            layouts.pick_on_image(self.new, "Capture", "rect", reference=self.old)
        self.assertEqual(len(calls), 2)
        self.assert_cleanup()


if __name__ == "__main__":
    unittest.main()
