"""Startup and interruption checks using fake Playwright objects, without GUI."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import playwrightscriptlib as psl


class StartupDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        for name in ("_pw", "_browser", "_page", "_layout", "_log"):
            patcher = mock.patch.object(psl, name, None)
            patcher.start()
            self.addCleanup(patcher.stop)
        # A diagnostic message must never take a screenshot or open step UI.
        for name in ("info", "viewportGrab", "alarm"):
            patcher = mock.patch.object(
                psl, name, side_effect=AssertionError("Unexpected %s" % name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_connect_reports_each_boundary_without_screenshots(self):
        events = []
        page = SimpleNamespace(url="https://remotedesktop.google.com/access/test")
        browser = SimpleNamespace(contexts=[SimpleNamespace(pages=[page])])

        def attach(url):
            events.append("attach")
            self.assertEqual(url, "http://127.0.0.1:9222")
            return browser

        driver = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=attach))

        def start():
            events.append("start")
            return driver

        with mock.patch.object(psl, "_emit", side_effect=events.append), \
                mock.patch.object(psl, "sync_playwright", return_value=SimpleNamespace(start=start)):
            result = psl.connect("http://127.0.0.1:9222", page_hint="remotedesktop.google.com")

        self.assertIs(result, page)
        self.assertEqual(events, [
            "Starting Playwright driver", "start", "Playwright driver started",
            "Connecting to browser debug endpoint", "attach",
            "Browser debug connection established", "Browser tab selected",
        ])

    def test_failed_driver_start_never_reports_connection_or_tab(self):
        error = RuntimeError("Driver did not start")
        starter = mock.Mock()
        starter.start.side_effect = error
        with mock.patch.object(psl, "_emit") as emit, \
                mock.patch.object(psl, "sync_playwright", return_value=starter):
            with self.assertRaises(RuntimeError) as caught:
                psl.connect("http://127.0.0.1:9222")
        self.assertIs(caught.exception, error)
        emit.assert_called_once_with("Starting Playwright driver")
        self.assertIsNone(psl._pw)

    def test_failed_connection_reports_last_stage_and_keeps_cleanup(self):
        error = RuntimeError("CDP connection timed out")
        driver = mock.Mock()
        driver.chromium.connect_over_cdp.side_effect = error
        starter = SimpleNamespace(start=lambda: driver)
        with mock.patch.object(psl, "_emit") as emit, \
                mock.patch.object(psl, "sync_playwright", return_value=starter):
            with self.assertRaises(RuntimeError) as caught:
                psl.connect("http://127.0.0.1:9222")
        self.assertIs(caught.exception, error)
        self.assertEqual(emit.call_args_list[-1], mock.call("Connecting to browser debug endpoint"))
        driver.stop.assert_called_once_with()
        self.assertIsNone(psl._pw)
        self.assertIsNone(psl._page)

    def test_folder_picker_is_announced_before_it_blocks_or_cancels(self):
        events = []

        def cancel():
            events.append("picker")
            raise psl._layouts.LayoutCancelled()

        with mock.patch.object(psl, "_emit", side_effect=events.append), \
                mock.patch.object(psl._layouts, "choose_layout_folder", side_effect=cancel):
            with self.assertRaises(psl._layouts.LayoutCancelled):
                psl.loadLayout()
        self.assertEqual(events, [
            "Opening screen layout folder picker; waiting for operator selection", "picker",
        ])
        self.assertIsNone(psl._layout)

    def test_ctrl_c_reports_to_console_and_log_without_alarm(self):
        class OperatorInterrupt(KeyboardInterrupt):
            pass

        for interrupt_type in (KeyboardInterrupt, OperatorInterrupt):
            with self.subTest(interrupt_type=interrupt_type):
                stdout, log = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), mock.patch.object(psl, "_log", log), \
                        mock.patch.object(sys, "excepthook"):
                    psl.alarmOnError()
                    sys.excepthook(interrupt_type, interrupt_type(), None)
                self.assertIn("Script interrupted by operator (Ctrl+C)", stdout.getvalue())
                self.assertEqual(log.getvalue(), stdout.getvalue())
        psl.alarm.assert_not_called()

    def test_other_uncaught_errors_still_log_traceback_and_alarm(self):
        error = RuntimeError("Position could not be read")
        log = io.StringIO()
        with mock.patch.object(sys, "excepthook"), \
                mock.patch.object(psl, "_log", log), \
                mock.patch.object(psl, "alarm") as alarm, \
                mock.patch.object(psl.traceback, "print_exception") as print_exception:
            psl.alarmOnError()
            sys.excepthook(RuntimeError, error, None)
        print_exception.assert_called_once_with(RuntimeError, error, None)
        self.assertIn("RuntimeError: Position could not be read", log.getvalue())
        alarm.assert_called_once_with("Script error: RuntimeError: Position could not be read")


if __name__ == "__main__":
    unittest.main()
