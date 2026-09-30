"""Offline regressions for named layouts and the existing numeric API.

No test connects to a browser or opens a picker. Mouse methods, screenshots,
operator dialogs, alarms and sleeps are replaced by mocks.
"""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image


LIBRARY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LIBRARY_ROOT))
import playwrightscriptlib as psl
import playwrightscriptlayout as layout


class NamedLayoutApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "screen-layout"
        self.folder.mkdir()
        self.write_json("layout.json", {"version": 1, "viewport": [640, 480]})
        self.page = mock.Mock()
        self.screen = Image.new("RGB", (640, 480), "white")
        self.start_patch(psl, "_page", self.page)
        self.start_patch(psl, "_layout", None)
        self.start_patch(psl, "_clicks_settle_time", 0)
        self.start_patch(psl, "viewportSize", return_value=(640, 480))
        self.start_patch(psl, "viewportGrab", side_effect=lambda: self.screen.copy())
        self.start_patch(psl, "_emit")
        self.start_patch(psl, "alarm", side_effect=AssertionError("Unexpected alarm"))
        self.start_patch(psl, "_VerifyPopup", side_effect=RuntimeError("Headless test"))
        self.start_patch(psl.time, "sleep")
        self.start_patch(layout, "choose_layout_folder",
                         side_effect=AssertionError("Unexpected folder picker"))
        self.start_patch(layout, "prompt_definition",
                         side_effect=AssertionError("Unexpected definition prompt"))
        self.start_patch(layout, "pick_on_image",
                         side_effect=AssertionError("Unexpected image picker"))
        self.start_patch(layout, "confirm_reference",
                         side_effect=AssertionError("Unexpected reference confirmation"))

    def start_patch(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def write_json(self, filename, value):
        (self.folder / filename).write_text(json.dumps(value), encoding="utf-8")

    def point(self, name="alignment-tab", x=12, y=34):
        self.write_json(name + ".json", {"version": 1, "kind": "point", "x": x, "y": y})

    def region(self, name="actual-xy", box=(10, 20, 110, 40)):
        self.write_json(name + ".json", {"version": 1, "kind": "region", "box": list(box)})

    def frame(self, name="focus-ready", box=(10, 20, 110, 40), color="white"):
        image_name = name + ".png"
        Image.new("RGB", (box[2] - box[0], box[3] - box[1]), color).save(self.folder / image_name)
        self.write_json(name + ".json", {"version": 1, "kind": "frame", "box": list(box),
                                         "image": image_name})
        return self.folder / image_name

    def load(self):
        return psl.loadLayout(self.folder)

    def allow_definition(self, selection):
        layout.prompt_definition.side_effect = None
        layout.prompt_definition.return_value = True
        layout.pick_on_image.side_effect = None
        layout.pick_on_image.return_value = selection
        layout.confirm_reference.side_effect = None
        layout.confirm_reference.return_value = True

    def test_import_has_no_browser_connection_or_tk_window(self):
        spec = importlib.util.spec_from_file_location("_layout_import_check",
                                                     LIBRARY_ROOT / "playwrightscriptlib.py")
        isolated = importlib.util.module_from_spec(spec)
        with mock.patch("tkinter.Tk") as tk, mock.patch("playwright.sync_api.sync_playwright") as pw:
            spec.loader.exec_module(isolated)
        tk.assert_not_called()
        pw.assert_not_called()
        layout.choose_layout_folder.assert_not_called()

    def test_load_explicit_folder_does_not_open_picker(self):
        self.load()
        layout.choose_layout_folder.assert_not_called()
        layout.prompt_definition.assert_not_called()
        self.page.mouse.click.assert_not_called()

    def test_load_without_path_uses_folder_picker(self):
        layout.choose_layout_folder.side_effect = None
        layout.choose_layout_folder.return_value = str(self.folder)
        self.point()
        psl.loadLayout()
        psl.click("alignment-tab")
        layout.choose_layout_folder.assert_called_once_with()
        self.page.mouse.click.assert_called_once_with(12, 34)

    def test_first_named_call_automatically_loads_folder(self):
        layout.choose_layout_folder.side_effect = None
        layout.choose_layout_folder.return_value = str(self.folder)
        self.point()
        psl.click("alignment-tab")
        layout.choose_layout_folder.assert_called_once_with()
        self.page.mouse.click.assert_called_once_with(12, 34)

    def test_named_click_and_doubleclick_use_saved_point(self):
        self.point()
        self.load()
        psl.click("alignment-tab")
        psl.doubleClick("alignment-tab")
        self.page.mouse.click.assert_called_once_with(12, 34)
        self.page.mouse.dblclick.assert_called_once_with(12, 34)
        layout.prompt_definition.assert_not_called()

    def test_missing_point_is_saved_then_clicks_exactly_once(self):
        self.load()
        self.allow_definition((81, 93))
        psl.click("alignment-tab")
        self.page.mouse.click.assert_called_once_with(81, 93)
        self.page.mouse.dblclick.assert_not_called()
        saved = json.loads((self.folder / "alignment-tab.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["kind"], saved["x"], saved["y"]), ("point", 81, 93))
        psl.click("alignment-tab")
        self.assertEqual(self.page.mouse.click.call_count, 2)
        self.assertEqual(layout.prompt_definition.call_count, 1)
        self.assertEqual(layout.pick_on_image.call_count, 1)

    def test_cancel_missing_point_prevents_actuation_and_save(self):
        self.load()
        self.allow_definition(None)
        with self.assertRaises(layout.LayoutCancelled) as caught:
            psl.click("alignment-tab")
        self.assertEqual(caught.exception.code, 2)
        self.page.mouse.click.assert_not_called()
        self.assertFalse((self.folder / "alignment-tab.json").exists())

    def test_failed_point_save_prevents_actuation(self):
        self.load()
        self.allow_definition((81, 93))
        with mock.patch.object(layout, "_write_json", side_effect=layout.LayoutError("Disk full")):
            with self.assertRaises(layout.LayoutError):
                psl.click("alignment-tab")
        self.page.mouse.click.assert_not_called()
        self.assertFalse((self.folder / "alignment-tab.json").exists())

    def test_cancel_folder_selection_prevents_actuation(self):
        layout.choose_layout_folder.side_effect = layout.LayoutCancelled()
        with self.assertRaises(layout.LayoutCancelled):
            psl.click("alignment-tab")
        self.page.mouse.click.assert_not_called()

    def test_named_grab_and_bounds_use_region_without_reference_image(self):
        self.region()
        self.load()
        self.assertEqual(tuple(psl.layoutBounds("actual-xy")), (10, 20, 110, 40))
        self.assertEqual(psl.frameGrab("actual-xy").size, (100, 20))
        self.assertEqual(list(self.folder.glob("*.png")), [])
        self.page.mouse.click.assert_not_called()

    def test_missing_grab_region_is_persisted_without_reference_confirmation(self):
        self.load()
        self.allow_definition((15, 25, 115, 45))
        self.assertEqual(psl.frameGrab("actual-xy").size, (100, 20))
        saved = json.loads((self.folder / "actual-xy.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kind"], "region")
        self.assertEqual(saved["box"], [15, 25, 115, 45])
        self.assertNotIn("image", saved)
        layout.confirm_reference.assert_not_called()

    def test_explicit_point_redefinition_does_not_click(self):
        self.point()
        self.load()
        self.allow_definition((51, 62))
        psl.definePoint("alignment-tab")
        self.page.mouse.click.assert_not_called()
        self.page.mouse.dblclick.assert_not_called()
        psl.click("alignment-tab")
        self.page.mouse.click.assert_called_once_with(51, 62)

    def test_missing_reference_requires_acceptance_and_runs_verification(self):
        self.load()
        self.allow_definition((10, 20, 110, 40))
        self.assertTrue(psl.verifyFrame("focus-ready"))
        layout.confirm_reference.assert_called_once()
        saved = json.loads((self.folder / "focus-ready.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["kind"], "frame")
        self.assertTrue((self.folder / saved["image"]).is_file())
        self.page.mouse.click.assert_not_called()

    def test_reference_rejection_saves_nothing_and_stops(self):
        self.load()
        self.allow_definition((10, 20, 110, 40))
        layout.confirm_reference.return_value = False
        with self.assertRaises(layout.LayoutCancelled):
            psl.verifyFrame("focus-ready")
        self.assertFalse((self.folder / "focus-ready.json").exists())
        self.assertEqual(list(self.folder.glob("*.png")), [])

    def test_named_reference_path_is_absolute_within_layout(self):
        expected = self.frame()
        self.load()
        with mock.patch.object(psl, "loadFrame", wraps=psl.loadFrame) as load_frame:
            self.assertTrue(psl.verifyFrame("focus-ready"))
        loaded = Path(load_frame.call_args.args[0])
        self.assertTrue(loaded.is_absolute())
        self.assertEqual(loaded.resolve(), expected.resolve())

    def test_named_verification_honors_delay_retry_and_threshold(self):
        self.frame()
        self.load()
        with mock.patch.object(psl, "frameSimilarity", side_effect=[0.90, 0.95, 0.99]) as compare:
            self.assertTrue(psl.verifyFrame("focus-ready", matchLevel=0.98,
                                            message="Waiting for focus", delay=2, retrycount=3))
        self.assertEqual(compare.call_count, 3)
        self.assertEqual(psl.time.sleep.call_args_list, [mock.call(2.0)] * 3)
        psl.alarm.assert_not_called()

    def test_named_verification_fallthrough_is_false_without_alarm(self):
        self.frame(color="black")
        self.load()
        self.assertFalse(psl.verifyFrame("focus-ready", matchLevel=0.98, fallthru=True))
        psl.alarm.assert_not_called()

    def test_named_verification_skip_returns_false(self):
        self.frame(color="black")
        self.load()
        psl.alarm.side_effect = None
        psl.alarm.return_value = 2
        self.assertFalse(psl.verifyFrame("focus-ready", message="Focus did not finish"))
        self.assertIn("Focus did not finish", psl.alarm.call_args.args[0])

    def test_named_actions_recheck_viewport_before_actuation(self):
        self.point()
        self.load()
        psl.viewportSize.return_value = (800, 600)
        with self.assertRaises(layout.LayoutError):
            psl.click("alignment-tab")
        self.page.mouse.click.assert_not_called()
        self.page.set_viewport_size.assert_not_called()
        psl.alarm.assert_not_called()

    def test_resize_during_point_definition_prevents_click(self):
        self.load()
        self.allow_definition((81, 93))

        def resize_while_selecting(*args):
            psl.viewportSize.return_value = (800, 600)
            return (81, 93)

        layout.pick_on_image.side_effect = resize_while_selecting
        with self.assertRaises(layout.LayoutError):
            psl.click("alignment-tab")
        self.page.mouse.click.assert_not_called()

    def test_resize_during_verification_delay_prevents_false_match(self):
        self.frame()
        self.load()

        def resize_while_waiting(seconds):
            psl.viewportSize.return_value = (800, 600)

        psl.time.sleep.side_effect = resize_while_waiting
        with mock.patch.object(psl, "frameSimilarity") as compare:
            with self.assertRaises(layout.LayoutError):
                psl.verifyFrame("focus-ready", delay=2, fallthru=True)
        compare.assert_not_called()

    def test_layout_viewport_remains_strict_and_noarg_check_accepts_match(self):
        self.load()
        self.assertEqual(tuple(psl.layoutViewport()), (640, 480))
        self.assertEqual(psl.checkViewport(), (640, 480))
        psl.viewportSize.return_value = (800, 600)
        with self.assertRaises(layout.LayoutError):
            psl.layoutViewport()
        psl.alarm.assert_not_called()

    def test_corrupt_region_is_not_cropped_or_redefined(self):
        self.region(box=(110, 20, 10, 40))
        self.load()
        with self.assertRaises(layout.LayoutError):
            psl.frameGrab("actual-xy")
        layout.prompt_definition.assert_not_called()

    def test_out_of_bounds_point_prevents_actuation(self):
        self.point(x=640, y=20)
        self.load()
        with self.assertRaises(layout.LayoutError):
            psl.click("alignment-tab")
        self.page.mouse.click.assert_not_called()

    def test_wrong_entry_kind_does_not_silently_redefine(self):
        self.region("alignment-tab")
        self.load()
        with self.assertRaises(layout.LayoutError):
            psl.click("alignment-tab")
        layout.prompt_definition.assert_not_called()
        self.page.mouse.click.assert_not_called()

    def test_wrong_reference_dimensions_do_not_rescale_and_pass(self):
        expected = self.frame()
        Image.new("RGB", (101, 20), "white").save(expected)
        self.load()
        with self.assertRaises(layout.LayoutError):
            psl.verifyFrame("focus-ready", fallthru=True)

    def test_jpeg_reference_is_rejected_even_with_png_extension(self):
        expected = self.frame()
        Image.new("RGB", (100, 20), "white").save(expected, format="JPEG")
        self.load()
        with self.assertRaises(layout.LayoutError):
            psl.verifyFrame("focus-ready", fallthru=True)

    def test_legacy_clicks_do_not_open_layout_picker(self):
        psl.click(17, 29)
        psl.doubleClick(31, 43)
        self.page.mouse.click.assert_called_once_with(17, 29)
        self.page.mouse.dblclick.assert_called_once_with(31, 43)
        layout.choose_layout_folder.assert_not_called()

    def test_legacy_grab_and_verification_keep_positional_call(self):
        expected = self.frame()
        self.assertEqual(psl.frameGrab(10, 20, 110, 40).size, (100, 20))
        self.assertTrue(psl.verifyFrame(str(expected), (10, 20, 110, 40), 0.98,
                                        "Legacy frame", 0, 1, True))
        layout.choose_layout_folder.assert_not_called()

    def test_legacy_viewport_can_continue_after_mismatch(self):
        psl.alarm.side_effect = None
        psl.alarm.return_value = 2
        psl.checkViewport(1280, 720)
        psl.alarm.assert_called_once()
        layout.choose_layout_folder.assert_not_called()


class LayoutDialogOwnershipTests(unittest.TestCase):
    """Dialog ownership is checked without creating actual Tk windows."""

    def test_existing_operator_root_gets_modal_child_with_local_grab(self):
        import tkinter as tk
        parent, child = mock.Mock(), mock.Mock()
        with mock.patch.object(tk, "_default_root", parent), \
                mock.patch.object(tk, "Toplevel", return_value=child) as toplevel, \
                mock.patch.object(tk, "Tk") as root_constructor:
            self.assertIs(layout._root("Define layout item"), child)
        root_constructor.assert_not_called()
        toplevel.assert_called_once_with(parent)
        child.transient.assert_called_once_with(parent)
        child.grab_set.assert_called_once_with()
        child.grab_set_global.assert_not_called()

    def test_standalone_dialog_creates_its_own_root(self):
        import tkinter as tk
        root = mock.Mock()
        with mock.patch.object(tk, "_default_root", None), \
                mock.patch.object(tk, "Toplevel") as toplevel, \
                mock.patch.object(tk, "Tk", return_value=root) as root_constructor:
            self.assertIs(layout._root("Define layout item"), root)
        root_constructor.assert_called_once_with()
        toplevel.assert_not_called()
        root.grab_set_global.assert_not_called()

    def test_failed_child_grab_closes_dialog_and_stops(self):
        import tkinter as tk
        parent, child = mock.Mock(), mock.Mock()
        child.grab_set.side_effect = tk.TclError("grab failed")
        with mock.patch.object(tk, "_default_root", parent), \
                mock.patch.object(tk, "Toplevel", return_value=child):
            with self.assertRaises(layout.LayoutError):
                layout._root("Define layout item")
        child.destroy.assert_called_once_with()

    def test_windows_parent_is_restored_only_when_modal_child_is_destroyed(self):
        import tkinter as tk
        parent, child = mock.Mock(), mock.Mock()
        parent.attributes.return_value = 0
        with mock.patch.object(tk, "_default_root", parent), \
                mock.patch.object(tk, "Toplevel", return_value=child), \
                mock.patch.object(layout.os, "name", "nt"):
            layout._root("Define layout item")
        parent.attributes.assert_any_call("-disabled", True)
        on_destroy = child.bind.call_args.args[1]
        parent.attributes.reset_mock()
        on_destroy(mock.Mock(widget=mock.Mock()))
        parent.attributes.assert_not_called()
        on_destroy(mock.Mock(widget=child))
        parent.attributes.assert_called_once_with("-disabled", 0)

    def test_windows_previously_disabled_owner_stays_disabled(self):
        import tkinter as tk
        parent, child = mock.Mock(), mock.Mock()
        parent.attributes.return_value = 1
        with mock.patch.object(tk, "_default_root", parent), \
                mock.patch.object(tk, "Toplevel", return_value=child), \
                mock.patch.object(layout.os, "name", "nt"):
            layout._root("Define layout item")
        on_destroy = child.bind.call_args.args[1]
        parent.attributes.reset_mock()
        on_destroy(mock.Mock(widget=child))
        parent.attributes.assert_called_once_with("-disabled", 1)


if __name__ == "__main__":
    unittest.main()
