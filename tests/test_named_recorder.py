"""Check generated named calls without connecting or clicking a real browser."""

import ast
from pathlib import Path
import sys
import unittest
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptrecord as recorder


class NamedRecorderTests(unittest.TestCase):
    def test_named_clicks_define_then_execute_once(self):
        for double in (False, True):
            with self.subTest(double=double):
                events = []
                writer = mock.Mock()
                with mock.patch("builtins.input", side_effect=["Comment", "alignment-tab"]), \
                        mock.patch.object(recorder.psl, "definePoint", side_effect=lambda name: events.append(("define", name))), \
                        mock.patch.object(recorder.psl, "click", side_effect=lambda name: events.append(("click", name))), \
                        mock.patch.object(recorder.psl, "doubleClick", side_effect=lambda name: events.append(("doubleClick", name))):
                    recorder.do_click(writer, double=double, named=True)
                action = "doubleClick" if double else "click"
                self.assertEqual(events, [("define", "alignment-tab"), (action, "alignment-tab")])
                code = writer.action.call_args.args[-1]
                self.assertEqual(code, "psl.%s('alignment-tab')" % action)
                ast.parse(code)

    def test_canceled_definition_never_clicks_or_records(self):
        writer = mock.Mock()
        with mock.patch("builtins.input", side_effect=["", "move-absolute"]), \
                mock.patch.object(recorder.psl, "definePoint", side_effect=recorder.layouts.LayoutCancelled()), \
                mock.patch.object(recorder.psl, "click") as click:
            with self.assertRaises(recorder.layouts.LayoutCancelled):
                recorder.do_click(writer, double=False, named=True)
        click.assert_not_called()
        writer.action.assert_not_called()

    def test_named_screen_keeps_threshold_message_and_retry_options(self):
        writer = mock.Mock()
        with mock.patch("builtins.input", side_effect=["", "focus-ready", "0.99", "Not ready", "10", "24"]), \
                mock.patch.object(recorder.psl, "defineFrame", return_value=("C:/layout/ref.png", (10, 20, 50, 40))) as define, \
                mock.patch.object(recorder.psl, "verifyFrame") as verify:
            recorder.do_screen_test(writer, "script", ".", named=True)
        define.assert_called_once_with("focus-ready")
        verify.assert_not_called()
        code = writer.action.call_args.args[-1]
        self.assertEqual(code, "psl.verifyFrame('focus-ready', matchLevel=0.99, message='Not ready', delay=10, retrycount=24)")
        ast.parse(code)

    def test_legacy_click_still_records_literal_coordinates(self):
        writer = mock.Mock()
        with mock.patch("builtins.input", return_value=""), \
                mock.patch.object(recorder.psl, "viewportGrab", return_value=Image.new("RGB", (100, 100))), \
                mock.patch.object(recorder, "_run_picker", return_value=(7, 8)), \
                mock.patch.object(recorder.psl, "click") as click, \
                mock.patch.object(recorder.psl, "definePoint") as define:
            recorder.do_click(writer, double=False)
        click.assert_called_once_with(7, 8)
        define.assert_not_called()
        self.assertEqual(writer.action.call_args.args[-1], "psl.click(7, 8)")

    def test_generated_header_selects_layout_without_hardcoded_viewport(self):
        writer = mock.Mock()
        with mock.patch("builtins.input", side_effect=["", "n", "", "", "6"]), \
                mock.patch.object(recorder, "ConsoleWriter", return_value=writer), \
                mock.patch.object(recorder.psl, "connect"), \
                mock.patch.object(recorder.psl, "disconnect"), \
                mock.patch.object(recorder.psl, "listPages", return_value=[(0, "Demo", "https://example.com")]), \
                mock.patch.object(recorder.psl, "viewportSize", return_value=(640, 480)), \
                mock.patch.object(recorder.psl, "loadLayout") as load, \
                mock.patch.object(recorder.psl, "checkViewport"):
            recorder.main()
        load.assert_called_once_with()
        header = writer.header.call_args.args[0]
        self.assertIn("psl.loadLayout(sys.argv[2] if len(sys.argv) > 2 else None)", header)
        self.assertIn("psl.checkViewport()", header)
        self.assertFalse(any("640, 480" in line for line in header))
        ast.parse("\n".join(header))


if __name__ == "__main__":
    unittest.main()
