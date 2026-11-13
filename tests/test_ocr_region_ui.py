"""OCR review UI regressions using fake Tk; no windows or machine input."""

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
        self.bindings, self.protocols, self.items = {}, {}, []
        self.destroyed = False
        self.contents = ""
        harness.widgets.append(self)

    def pack(self, *args, **kwargs):
        pass

    grid = rowconfigure = columnconfigure = lift = pack
    xview = yview = set = pack

    def configure(self, **options):
        self.options.update(options)

    def winfo_screenwidth(self):
        return 1280

    def winfo_screenheight(self):
        return 900

    def insert(self, position, text):
        self.contents += text

    def create_image(self, *coords, **options):
        self.items.append(options["image"])

    def bind(self, event, callback):
        self.bindings[event] = callback

    def protocol(self, event, callback):
        self.protocols[event] = callback

    def wait_window(self, window):
        self.harness.action(self)
        if not self.destroyed:
            raise AssertionError("Planned UI action did not close the dialog")

    def destroy(self):
        if self.destroyed:
            raise FakeTclError("already destroyed")
        self.destroyed = True


class Harness:
    def __init__(self):
        self.widgets, self.roots = [], []
        self.action = lambda root: root.destroy()
        self.tk = SimpleNamespace(TclError=FakeTclError)
        for kind in ("Frame", "Label", "Text", "Canvas", "Scrollbar", "Button"):
            setattr(self.tk, kind, self.factory(kind))

    def factory(self, kind):
        return lambda parent=None, **options: Widget(self, kind, parent, **options)

    def root(self, title):
        root = Widget(self, "Root", title=title)
        self.roots.append(root)
        return root

    def click(self, label):
        button = next(widget for widget in self.widgets
                      if widget.kind == "Button" and widget.options["text"] == label)
        button.options["command"]()


class OCRRegionUITests(unittest.TestCase):
    def setUp(self):
        self.h = Harness()
        self.crop = Image.new("RGB", (1400, 500), (17, 33, 49))
        self.patch(mock.patch.dict(sys.modules, {"tkinter": self.h.tk}))
        self.roots = self.patch(mock.patch.object(layouts, "_root", side_effect=self.h.root))
        self.photos = self.patch(mock.patch.object(
            ImageTk, "PhotoImage", side_effect=lambda image, master: image.copy()))

    def patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def reset_dialog(self):
        self.h.widgets.clear()
        self.h.roots.clear()

    def test_current_crop_and_literal_ocr_text_are_available_before_next(self):
        raw = " X=-71222.3, Y=0 µm\n"

        def review(root):
            canvas = next(widget for widget in self.h.widgets if widget.kind == "Canvas")
            image = canvas.items[0]
            self.assertEqual(image.size, self.crop.size)
            self.assertEqual(image.tobytes(), self.crop.tobytes())
            self.assertLess(canvas.options["width"], image.width)
            self.assertEqual(canvas.options["scrollregion"], (0, 0, *self.crop.size))
            text = next(widget for widget in self.h.widgets if widget.kind == "Text")
            self.assertEqual(text.contents, repr(raw))
            self.assertEqual(text.options["state"], "disabled")
            self.h.click("Next Step")

        self.h.action = review
        self.assertEqual(layouts.ocr_dialog("actual-xy", self.crop, raw), 0)
        self.assertTrue(self.h.roots[0].destroyed)

    def test_caller_buttons_return_their_exact_index(self):
        options = (
            ("Next Step", "Run Continuously", "Redefine OCR area", "STOP"),
            ("Retry", "Redefine OCR area", "STOP"),
            ("Save OCR area", "Select again", "Cancel"),
            ("Select again", "Cancel"),
        )
        for buttons in options:
            for index, label in enumerate(buttons):
                with self.subTest(buttons=buttons, choice=label):
                    self.reset_dialog()
                    self.h.action = lambda root, label=label: self.h.click(label)
                    result = layouts.ocr_dialog("actual-xy", self.crop, "X=0, Y=0 um",
                                                buttons=buttons)
                    self.assertEqual(result, index)
                    self.assertTrue(self.h.roots[0].destroyed)

    def test_escape_and_window_close_select_stop_or_cancel(self):
        for buttons in (("Next Step", "Run Continuously", "Redefine OCR area", "STOP"),
                        ("Retry", "Redefine OCR area", "STOP"),
                        ("Save OCR area", "Select again", "Cancel"),
                        ("Select again", "Cancel")):
            for event in ("escape", "close"):
                with self.subTest(buttons=buttons, event=event):
                    self.reset_dialog()

                    def close(root, event=event):
                        if event == "escape":
                            root.bindings["<Escape>"](None)
                        else:
                            root.protocols["WM_DELETE_WINDOW"]()

                    self.h.action = close
                    self.assertEqual(layouts.ocr_dialog("actual-xy", self.crop, "",
                                                        buttons=buttons), len(buttons) - 1)
                    self.assertTrue(self.h.roots[0].destroyed)

    def test_error_is_shown_with_raw_result_and_no_unrequested_save_button(self):
        self.h.action = lambda root: self.h.click("Select again")
        self.assertEqual(layouts.ocr_dialog("actual-xy", self.crop, "wrong text",
                                            error="Expected X/Y numbers",
                                            buttons=("Select again", "Cancel")), 0)
        labels = [w.options.get("text") for w in self.h.widgets if w.kind == "Label"]
        self.assertIn("Expected X/Y numbers", labels)
        buttons = [w.options["text"] for w in self.h.widgets if w.kind == "Button"]
        self.assertEqual(buttons, ["Select again", "Cancel"])

    def test_failure_without_pixels_can_still_be_stopped(self):
        self.h.action = lambda root: self.h.click("STOP")
        self.assertEqual(layouts.ocr_dialog("actual-xy", None, None,
                                            error="OCR unavailable",
                                            buttons=("Retry", "Redefine OCR area", "STOP")), 2)
        self.photos.assert_not_called()
        text = next(w for w in self.h.widgets if w.kind == "Text")
        self.assertEqual(text.contents, "(No OCR text returned)")

    def test_rendering_failure_closes_dialog_and_preserves_error(self):
        self.photos.side_effect = RuntimeError("image allocation failed")
        with self.assertRaisesRegex(RuntimeError, "image allocation failed"):
            layouts.ocr_dialog("actual-xy", self.crop, "X=0, Y=0 um")
        self.assertTrue(self.h.roots[0].destroyed)

    def test_text_widget_failure_closes_dialog_and_preserves_error(self):
        self.h.tk.Text = mock.Mock(side_effect=RuntimeError("text allocation failed"))
        with self.assertRaisesRegex(RuntimeError, "text allocation failed"):
            layouts.ocr_dialog("actual-xy", self.crop, "X=0, Y=0 um")
        self.assertTrue(self.h.roots[0].destroyed)

    def test_invalid_data_is_rejected_before_creating_window(self):
        for args, kwargs in (
            (("actual-xy", None, ""), {}),
            (("actual-xy", object(), ""), {}),
            (("actual-xy", self.crop, 123), {}),
            (("actual-xy", self.crop, ""), {"buttons": ()}),
            (("actual-xy", self.crop, ""), {"buttons": "STOP"}),
            (("actual-xy", self.crop, ""), {"buttons": ("",)}),
        ):
            with self.subTest(args=args, kwargs=kwargs):
                with self.assertRaises(layouts.LayoutError):
                    layouts.ocr_dialog(*args, **kwargs)
        self.roots.assert_not_called()


if __name__ == "__main__":
    unittest.main()
