"""Exercise the desktop first-run path through a simulated serial port."""

import time
import tkinter as tk
import unittest
from unittest.mock import patch

from serial.tools.list_ports_common import ListPortInfo

from hameg_studio import APP_NAME
from hameg_studio.app import SpectrumStudio
from hameg_studio.preferences import Preferences
from test_protocol import SimulatedPort, trace_frame


class DesktopWorkflowTests(unittest.TestCase):
    def wait_until(self, condition, *, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if self.callback_errors:
                self.fail(str(self.callback_errors))
            if condition():
                return
            time.sleep(0.01)
        self.fail(f"Timed out. Last application status: {self.app.message}")

    def start_app(self, model):
        self.root = tk.Tk()
        self.root.withdraw()
        self.callback_errors = []
        self.root.report_callback_exception = lambda _type, error, _trace: self.callback_errors.append(error)
        self.app = SpectrumStudio(self.root, preferences=Preferences(model=model))

    def stop_app(self):
        self.app.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.root.update()
                self.root.winfo_exists()
            except tk.TclError:
                return
            time.sleep(0.01)
        self.fail("Application did not shut down")

    def test_scan_connect_capture_and_control_rf_on_both_models(self):
        for model, level, payload in (
            ("HM5014-2", "-25,2", b"#tl-25.2\r"),
            ("HM5530", "-6.2", b"#tl-06.2\r"),
        ):
            with self.subTest(model=model):
                self.exercise_first_run(model, level, payload)

    def exercise_first_run(self, model, level, payload):
        device = SimulatedPort(model)
        if model == "HM5530":
            device.ignored_queries.add("#df")
        else:
            device.trace_reply = b"RD\r\n" + trace_frame()[:-1]
        with (
            patch("hameg_studio.app.list_ports.comports", return_value=[]) as ports,
            patch("hameg_studio.protocol.serial.Serial", return_value=device) as serial_port,
            patch.object(Preferences, "save"),
        ):
            self.start_app(model)
            try:
                self.assertEqual(self.root.title(), APP_NAME)
                self.assertTrue(self.app.connect_button.instate(("disabled",)))
                ports.return_value = [ListPortInfo("COM42")]
                self.app.scan_button.invoke()
                self.assertEqual(self.app.port.get(), "COM42")
                self.app.connect_button.invoke()
                self.wait_until(lambda: self.app.spectrum is not None)
                self.app.live_button.invoke()
                self.assertEqual(serial_port.call_args.kwargs["port"], "COM42")
                self.assertEqual(self.app.spectrum.frequency(0), 400)
                self.assertEqual(len(self.app.spectrum.samples), 2001)
                self.assertEqual(self.app.instrument["tg"], 0)
                self.assertFalse(any(command.startswith((b"#br", b"#tg1")) for command in device.writes))
                if model == "HM5530":
                    self.assertNotIn("df", self.app.instrument)
                    self.assertIn("readbacks unavailable", self.app.footer_info.cget("text"))

                wire_before_marking = device.writes.copy()
                self.assertTrue(self.app.marker_left_button.instate(("disabled",)))
                self.assertTrue(self.app.marker_right_button.instate(("disabled",)))
                self.app.peak_button.invoke()
                peak = self.app.marker
                peak_readout = self.app.marker_status.cget("text")
                self.app.marker_right_button.invoke()
                self.assertEqual(self.app.marker, peak + 1)
                self.assertNotEqual(self.app.marker_status.cget("text"), peak_readout)
                self.app.marker_left_button.invoke()
                self.assertEqual(self.app.marker, peak)
                self.assertEqual(self.app.marker_status.cget("text"), peak_readout)
                self.app.plot.select_marker(0)
                self.assertTrue(self.app.marker_left_button.instate(("disabled",)))
                self.app.plot.select_marker(len(self.app.spectrum.samples) - 1)
                self.assertTrue(self.app.marker_right_button.instate(("disabled",)))
                self.app.clear_button.invoke()
                self.assertIsNone(self.app.marker)
                self.assertTrue(self.app.marker_left_button.instate(("disabled",)))
                self.assertTrue(self.app.marker_right_button.instate(("disabled",)))
                self.assertEqual(device.writes, wire_before_marking)

                original_trace = self.app.spectrum
                self.app.average_button.invoke()
                self.wait_until(self.app.average_active)
                self.assertFalse(self.app.live)
                averaged_samples = bytes([97] * 2001)
                device.trace_reply = trace_frame(samples=averaged_samples)
                self.app.capture_button.invoke()
                self.wait_until(lambda: self.app.spectrum.mode == 3)
                self.assertEqual(self.app.spectrum.samples, tuple(averaged_samples))
                self.assertNotEqual(self.app.spectrum.samples, original_trace.samples)
                self.app.average_button.invoke()
                self.wait_until(lambda: self.app.instrument.get("vm") == 0)
                self.assertFalse(self.app.average_active())
                prior_trace = self.app.spectrum
                wire_before = device.writes.copy()
                self.app.focus_button.invoke()
                self.app.intens_button.invoke()
                self.assertEqual((self.app.preferences.trace_focus, self.app.preferences.trace_intensity),
                                 ("sharp", 70))
                self.assertIs(self.app.spectrum, prior_trace)
                self.assertEqual(device.writes, wire_before)
                if model == "HM5014-2":
                    self.assertEqual(self.app.instrument.get("dm"), 0)
                    self.app.test_signal_button.invoke()
                    self.assertTrue(self.app.test_tune_button.instate(("!disabled",)))
                    self.app.test_tune_button.invoke()
                    self.wait_until(lambda: self.app.instrument.get("cf") == 48)
                    tuning_writes = device.writes[len(wire_before):]
                    self.assertIn(b"#cf0048.000\r", tuning_writes)
                    self.assertLess(tuning_writes.index(b"#tg\r"), tuning_writes.index(b"#cf0048.000\r"))
                    self.assertNotIn(b"#tg1\r", tuning_writes)
                    self.assertNotIn(b"#tg0\r", tuning_writes)
                    self.assertEqual(self.app.instrument.get("sp"), 200)
                else:
                    self.assertEqual(str(self.app.test_signal_button.cget("text")), "TEST SIGNAL ON")

                self.app.controls["tl"].variable.set(level)
                self.app.output_button.invoke()
                self.assertEqual(self.app.message[0], "set_output_level_first")
                self.assertNotIn(b"#tg1\r", device.writes)
                self.app.controls["tl"].apply_button.invoke()
                self.wait_until(lambda: self.app.instrument.get("tl") == float(level.replace(",", ".")))
                self.assertIn(payload, device.writes)
                self.app.output_button.invoke()
                self.wait_until(lambda: self.app.instrument.get("tg") == 1)
                if model == "HM5014-2":
                    self.app.test_signal_button.invoke()
                    self.assertTrue(self.app.test_tune_button.instate(("disabled",)))
                    self.app.dialog.destroy()
                else:
                    self.assertEqual(str(self.app.test_signal_button.cget("text")), "TEST SIGNAL OFF")
                self.app.output_button.invoke()
                self.wait_until(lambda: self.app.instrument.get("tg") == 0)
                self.app.connect_button.invoke()
                self.wait_until(lambda: self.app.analyzer is None)
                self.assertEqual(device.writes[-2:], [b"#kl0\r", b"#kl\r"])
                self.assertFalse(device.is_open)
            finally:
                self.stop_app()


if __name__ == "__main__":
    unittest.main()
