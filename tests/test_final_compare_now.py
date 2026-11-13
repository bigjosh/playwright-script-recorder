"""Offline checks for ending frame retries with one fresh comparison.

Synthetic images and fake popup windows keep these tests away from the browser,
desktop, keyboard, and machine controls.
"""

from pathlib import Path
import sys
import types
import unittest
from unittest import mock

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlib as psl


class FinalCompareNowTests(unittest.TestCase):
    def setUp(self):
        self.box = (10, 20, 30, 30)
        self.baseline = Image.new("RGB", (20, 10), "black")
        self.fresh = Image.new("RGB", (20, 10), "white")
        self.full = Image.new("RGB", (640, 480), "white")
        self.load = self.patch(psl, "loadFrame", return_value=self.baseline)
        self.capture = self.patch(psl, "_frame_and_full",
                                  return_value=(self.fresh, self.full))
        self.similarity = self.patch(psl, "frameSimilarity", return_value=0.5)
        self.emit = self.patch(psl, "_emit")
        self.patch(psl, "info")
        self.sleep = self.patch(psl.time, "sleep")
        self.alarm = self.patch(psl, "alarm", return_value=2)
        self.recapture = self.patch(psl, "_recapture_reference",
                                    side_effect=AssertionError("Unexpected recapture"))
        self.popup = self.new_popup("finalnow")
        self.popup_factory = self.patch(psl, "_VerifyPopup", return_value=self.popup)

    def patch(self, target, attribute, *args, **kwargs):
        patcher = mock.patch.object(target, attribute, *args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def new_popup(self, action=None):
        popup = mock.Mock()
        popup.countdown.return_value = action
        popup.note_grab.return_value = None
        return popup

    def verify(self, **options):
        arguments = dict(delay=5, retrycount=3, matchLevel=0.99)
        arguments.update(options)
        return psl.verifyFrame("reference.png", self.box, **arguments)

    def test_final_now_takes_fresh_comparison_and_keeps_threshold(self):
        # This score would pass the default 0.98, but must fail our 0.99.
        self.similarity.return_value = 0.985

        self.assertFalse(self.verify())

        self.capture.assert_called_once_with(*self.box)
        self.similarity.assert_called_once_with(self.baseline, self.fresh)
        self.popup.countdown.assert_called_once_with(5.0, 1, 3)
        self.popup.close.assert_called_once_with()
        self.sleep.assert_not_called()
        self.alarm.assert_called_once()
        detail = self.alarm.call_args.args[0]
        self.assertIn("similarity 0.9850 after 1 tries", detail)
        self.assertIn("needs at least 0.99", detail)
        self.assertIn("Final comparison requested", detail)
        self.assertEqual(self.alarm.call_args.kwargs["buttons"], (
            "Try compare again", "Show differences", "Skip and continue",
            "Recapture reference", "Stop the script"))

    def test_final_now_returns_true_when_fresh_comparison_passes(self):
        self.similarity.return_value = 0.995

        self.assertTrue(self.verify())

        self.capture.assert_called_once_with(*self.box)
        self.popup.countdown.assert_called_once_with(5.0, 1, 3)
        self.popup.close.assert_called_once_with()
        self.alarm.assert_not_called()

    def test_final_now_preserves_inclusive_match_threshold(self):
        self.similarity.return_value = 0.99
        self.assertTrue(self.verify())
        self.alarm.assert_not_called()

    def test_final_now_after_miss_uses_new_passing_score(self):
        self.popup.countdown.side_effect = [None, "finalnow"]
        self.similarity.side_effect = [0.1, 0.995]

        self.assertTrue(self.verify())

        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.popup.countdown.call_args_list, [
            mock.call(5.0, 1, 3), mock.call(5.0, 2, 3)])
        self.alarm.assert_not_called()

    def test_final_now_after_miss_reports_new_score_and_actual_attempts(self):
        self.popup.countdown.side_effect = [None, "finalnow"]
        self.similarity.side_effect = [0.1, 0.8]

        self.assertFalse(self.verify())

        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.popup.countdown.call_count, 2)
        detail = self.alarm.call_args.args[0]
        self.assertIn("similarity 0.8000 after 2 tries", detail)
        self.assertNotIn("0.1000", detail)

    def test_try_again_restores_entire_original_retry_cycle(self):
        retry_popup = self.new_popup()
        self.popup_factory.side_effect = [self.popup, retry_popup]
        self.alarm.side_effect = [0, 2]

        self.assertFalse(self.verify())

        self.assertEqual(self.capture.call_count, 4)
        self.popup.countdown.assert_called_once_with(5.0, 1, 3)
        self.assertEqual(retry_popup.countdown.call_args_list, [
            mock.call(5.0, 1, 3), mock.call(5.0, 2, 3), mock.call(5.0, 3, 3)])
        self.popup.close.assert_called_once_with()
        retry_popup.close.assert_called_once_with()
        self.assertIn("Final comparison requested", self.alarm.call_args_list[0].args[0])
        self.assertNotIn("Final comparison requested", self.alarm.call_args_list[1].args[0])
        self.assertIn("after 3 tries", self.alarm.call_args_list[1].args[0])

    def test_recapture_rechecks_fresh_pixels_with_original_delay_and_retries(self):
        new_box = (40, 50, 70, 65)
        new_baseline = Image.new("RGB", (30, 15), "blue")
        retry_popup = self.new_popup()
        self.popup_factory.side_effect = [self.popup, retry_popup]
        self.alarm.return_value = 3
        self.recapture.side_effect = None
        self.recapture.return_value = ("replacement.png", new_box)
        self.load.side_effect = [self.baseline, new_baseline]
        self.similarity.side_effect = [0.5, 0.5, 0.5, 0.995]

        self.assertTrue(self.verify())

        self.recapture.assert_called_once_with(
            None, "reference.png", self.box, self.baseline)
        self.assertEqual(self.load.call_args_list, [
            mock.call("reference.png"), mock.call("replacement.png")])
        self.assertEqual(self.capture.call_args_list, [
            mock.call(*self.box), mock.call(*new_box), mock.call(*new_box),
            mock.call(*new_box)])
        self.assertEqual(self.similarity.call_args_list, [
            mock.call(self.baseline, self.fresh),
            mock.call(new_baseline, self.fresh), mock.call(new_baseline, self.fresh),
            mock.call(new_baseline, self.fresh)])
        self.assertEqual(retry_popup.countdown.call_args_list, [
            mock.call(5.0, 1, 3), mock.call(5.0, 2, 3), mock.call(5.0, 3, 3)])
        self.alarm.assert_called_once()
        retry_popup.close.assert_called_once_with()

    def test_final_now_with_fallthru_returns_false_without_alarm(self):
        self.assertFalse(self.verify(fallthru=True))
        self.capture.assert_called_once_with(*self.box)
        self.popup.countdown.assert_called_once_with(5.0, 1, 3)
        self.alarm.assert_not_called()
        self.recapture.assert_not_called()

    def test_try_now_still_retains_remaining_retries(self):
        self.popup.countdown.side_effect = ["trynow", None, None]

        self.assertFalse(self.verify(fallthru=True))

        self.assertEqual(self.capture.call_count, 3)
        self.assertEqual(self.popup.countdown.call_count, 3)
        self.alarm.assert_not_called()

    def test_abort_verify_does_not_capture_or_raise_mismatch_alarm(self):
        self.popup.countdown.return_value = "abortverify"
        self.assertFalse(self.verify())
        self.capture.assert_not_called()
        self.alarm.assert_not_called()
        self.popup.close.assert_called_once_with()

    def test_abort_script_does_not_capture_or_raise_mismatch_alarm(self):
        self.popup.countdown.return_value = "abortscript"
        with self.assertRaises(SystemExit) as stopped:
            self.verify()
        self.assertEqual(stopped.exception.code, 2)
        self.capture.assert_not_called()
        self.alarm.assert_not_called()
        self.popup.close.assert_called_once_with()

    def test_final_now_pressed_during_status_refresh_is_used_immediately(self):
        self.popup.countdown.return_value = None
        self.popup.note_grab.return_value = "finalnow"

        self.assertFalse(self.verify())

        self.capture.assert_called_once_with(*self.box)
        self.popup.countdown.assert_called_once_with(5.0, 1, 3)
        self.assertIn("Final comparison requested", self.alarm.call_args.args[0])

    def test_abort_during_status_refresh_takes_precedence_over_final_now(self):
        for action in ("abortverify", "abortscript"):
            with self.subTest(action=action):
                self.popup.note_grab.return_value = action
                if action == "abortscript":
                    with self.assertRaises(SystemExit) as stopped:
                        self.verify()
                    self.assertEqual(stopped.exception.code, 2)
                else:
                    self.assertFalse(self.verify())
        self.capture.assert_not_called()
        self.alarm.assert_not_called()


class FinalComparePopupTests(unittest.TestCase):
    def setUp(self):
        self.root = mock.Mock()
        self.root.winfo_screenwidth.return_value = 1920
        self.root.winfo_screenheight.return_value = 1080
        self.root.winfo_width.return_value = 600
        self.root.winfo_height.return_value = 240
        self.buttons = {}

        def make_button(parent, **options):
            self.buttons[options["text"]] = options["command"]
            return mock.Mock()

        fake_tk = types.SimpleNamespace(
            Tk=mock.Mock(return_value=self.root), Label=mock.Mock(),
            Frame=mock.Mock(), Button=mock.Mock(side_effect=make_button))
        patcher = mock.patch.dict(sys.modules, {"tkinter": fake_tk})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.popup = psl._VerifyPopup("focus-ready")

    def test_each_button_keeps_its_own_action(self):
        for label, action in (("Try now", "trynow"),
                              ("Final compare now", "finalnow"),
                              ("Abort verify", "abortverify"),
                              ("Abort script", "abortscript")):
            with self.subTest(label=label):
                self.buttons[label]()
                self.assertEqual(self.popup.action, action)
        self.assertEqual(self.root.quit.call_count, 4)

    def test_countdown_returns_final_action_and_clears_it(self):
        self.root.mainloop.side_effect = self.buttons["Final compare now"]

        self.assertEqual(self.popup.countdown(5, 1, 3), "finalnow")

        self.assertIsNone(self.popup.action)
        self.root.after_cancel.assert_called_once()

    def test_status_refresh_returns_pending_action_and_clears_it(self):
        self.root.update.side_effect = self.buttons["Final compare now"]

        self.assertEqual(self.popup.note_grab(1, 3), "finalnow")

        self.assertIsNone(self.popup.action)


if __name__ == "__main__":
    unittest.main()
