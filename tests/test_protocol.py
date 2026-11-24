"""Regression checks for the HAMEG profiles and RS-232 protocol."""

import unittest
from itertools import count
from unittest.mock import patch

from hameg_studio.protocol import (
    ModelMismatch, ProtocolError, ProtocolTimeout, SerialAnalyzer, Trace, TraceTransferError, parse_trace,
)
from hameg_studio.profiles import PROFILES


SAMPLES = bytes(index % 256 for index in range(2001))


def trace_frame(samples=SAMPLES, center=b"CF0500.000"):
    """Build an instrument-shaped frame with embedded control bytes."""
    frame = bytearray(2048)
    frame[:2001] = samples
    frame[2016:2026] = center
    frame[2044:2047] = sum(samples).to_bytes(3, "big")
    frame[2047] = 0x0D
    return bytes(frame)


class ProfileTests(unittest.TestCase):
    def test_hm5014_center_span_and_generator_commands(self):
        profile = PROFILES["HM5014-2"]
        commands = (
            ("cf", "0.000", "#cf0000.000"),
            ("cf", "1050.000", "#cf1050.000"),
            ("sp", 0, "#sp0"),
            ("sp", 200, "#sp200"),
            ("tg", 1, "#tg1"),
            ("tg", 0, "#tg0"),
            ("tl", -50, "#tl-50.0"),
            ("tl", 1, "#tl+01.0"),
        )
        for key, value, expected in commands:
            with self.subTest(key=key, value=value):
                self.assertEqual(profile.setting_command(key, value), expected)

    def test_hm5530_frequency_and_generator_commands(self):
        profile = PROFILES["HM5530"]
        commands = (
            ("cf", 3000, "#cf3000.000"),
            ("sp", 0, "#sp0000.000"),
            ("sp", "2.325", "#sp0002.325"),
            ("sr", 0, "#sr0000.000"),
            ("st", 3000, "#st3000.000"),
            ("tg", 1, "#tg1"),
            ("tg", 0, "#tg0"),
            ("tl", -10, "#tl-10.0"),
            ("tl", 0, "#tl+00.0"),
        )
        for key, value, expected in commands:
            with self.subTest(key=key, value=value):
                self.assertEqual(profile.setting_command(key, value), expected)

    def test_out_of_range_unsupported_and_off_step_values(self):
        invalid_settings = (
            ("HM5014-2", "cf", 1050.001),
            ("HM5014-2", "sp", 3),
            ("HM5014-2", "tl", -50.2),
            ("HM5014-2", "tl", 1.2),
            ("HM5014-2", "tl", -12.3),
            ("HM5014-2", "et", 1),
            ("HM5014-2", "uc", 0),
            ("HM5530", "cf", -0.001),
            ("HM5530", "sp", 0.999),
            ("HM5530", "tl", -10.2),
            ("HM5530", "tl", 0.2),
            ("HM5530", "tl", "nan"),
            ("HM5530", "tl", "Infinity"),
            ("HM5530", "tg", 2),
            ("HM5530", "sp", "2.1234"),
        )
        for model, key, value in invalid_settings:
            with self.subTest(model=model, key=key, value=value):
                with self.assertRaises(ValueError):
                    PROFILES[model].setting_command(key, value)

    def test_selected_hm5530_unit_converts_absolute_levels(self):
        profile = PROFILES["HM5530"]
        self.assertEqual(profile.setting_command("tl", -10, level_unit=1), "#tl+37.0")
        self.assertEqual(profile.setting_command("tl", 0, level_unit=2), "#tl+107.0")
        self.assertEqual(profile.parse_reply("tl", "TL+37.0\r", level_unit=1), -10)
        self.assertEqual(profile.parse_reply("tl", "TL+107.0\r", level_unit=2), 0)
        self.assertEqual(profile.parse_reply("lv", "ML+017.0\r", level_unit=1), -30)
        self.assertEqual(profile.parse_reply("lv", "DL-012.0\r", level_unit=1), -12)
        self.assertEqual(profile.parse_reply("rl", "rl -31.0\r"), -31)
        with self.assertRaises(ValueError):
            profile.parse_reply("tl", "RL-10.0\r")


class FrameTests(unittest.TestCase):
    def test_reads_2001_samples_and_fixed_center_field(self):
        trace = parse_trace(trace_frame(center=b"CF2750.125"))
        self.assertEqual(trace.samples, tuple(SAMPLES))
        self.assertEqual(trace.center_mhz, 2750.125)

    def test_accepts_missing_center_metadata(self):
        self.assertIsNone(parse_trace(trace_frame(center=bytes(10))).center_mhz)

    def test_rejects_bad_length_terminator_checksum_and_center(self):
        frame = trace_frame()
        broken_frames = {
            "short response": frame[:-1],
            "wrong terminator": frame[:-1] + b"\n",
            "wrong checksum": frame[:56] + b"\x00" + frame[57:],
            "malformed center": trace_frame(center=b"CFxxxx.xxx"),
        }
        for description, broken_frame in broken_frames.items():
            with self.subTest(description):
                with self.assertRaises(ProtocolError):
                    parse_trace(broken_frame)


class SimulatedPort:
    """Emulate the serial wire, including fixed-size binary data in small reads."""

    def __init__(self, model, *, device_baud=None, level_unit=0):
        self.model = model
        self.baudrate = PROFILES[model].default_baud
        self.device_baud = device_baud or self.baudrate
        self.is_open = True
        self.out_waiting = 0
        self.writes = []
        self.pending = bytearray()
        self.trace_reply = trace_frame()
        self.singles_sent = 0
        self.override_test_level = None
        self.ignore_remote_on = False
        self.ignore_remote_off = False
        self.ignored_queries = set()
        self.lost_acknowledgments = set()
        self.accept_baud_change = True
        self.settings = {
            "kl": "0", "cf": "0500.000", "sp": "0200.000", "rl": "-10.0",
            "at": "20", "db": "10", "bw": "120", "vf": "1", "vm": "0",
            "dm": "0", "tg": "0", "tl": "-8.0", "uc": "0",
            "du": str(level_unit), "sr": "0400.000", "st": "0600.000", "ra": "0",
            "ba": "0", "mk": "0", "mf": "0500.000", "df": "0100.000",
        }

    def read(self, size):
        read_size = min(size, 79, len(self.pending))
        payload = bytes(self.pending[:read_size])
        del self.pending[:read_size]
        return payload

    def reset_input_buffer(self):
        self.pending.clear()

    def close(self):
        self.is_open = False

    def write(self, payload):
        self.writes.append(payload)
        if not payload.startswith(b"#") or not payload.endswith(b"\r"):
            raise AssertionError(f"Invalid command framing: {payload!r}")
        if self.baudrate == self.device_baud:
            command = payload[:-1].decode("ascii").lower()
            self.pending.extend(self.reply_to(command))
        return len(payload)

    def reply_to(self, command):
        command_key = command[1:3]
        argument = command[3:]
        if command in self.ignored_queries:
            return b""
        if command == "#hm":
            return (self.model[2:] + "\r").encode("ascii")
        if command == "#vn":
            return b"1.23\r"
        if command == "#bm1":
            return self.trace_reply
        if command == "#ss1" and self.model == "HM5014-2":
            self.singles_sent += 1
            return self.trace_reply if self.singles_sent > 1 else bytes(2048)
        if command_key == "br":
            if self.accept_baud_change:
                self.device_baud = int(argument)
            return b""
        if command_key == "kl" and argument == "1" and self.ignore_remote_on:
            return b"RD\r"
        if command_key == "kl" and argument == "0" and self.ignore_remote_off:
            return b""
        if command_key in self.settings and not argument:
            return f"{command_key.upper()} {self.settings[command_key]}\r".encode("ascii")
        if command_key in self.settings:
            if command_key == "tl" and self.override_test_level is not None:
                argument = self.override_test_level
            self.settings[command_key] = argument
        return b"" if command in self.lost_acknowledgments else b"RD\r"


class SerialAnalyzerTests(unittest.TestCase):
    def connect(self, connection, *, model=None, fast=False):
        profile = PROFILES[model or connection.model]
        with patch("hameg_studio.protocol.serial.Serial", return_value=connection) as open_port:
            session = SerialAnalyzer.connect(profile, "COM42", fast=fast)
        self.assertEqual(open_port.call_args.kwargs["port"], "COM42")
        self.assertFalse(open_port.call_args.kwargs["xonxoff"])
        self.assertFalse(open_port.call_args.kwargs["rtscts"])
        self.assertFalse(open_port.call_args.kwargs["dsrdtr"])
        self.addCleanup(session.close)
        return session

    def test_identifies_and_reads_both_models_without_enabling_rf_output(self):
        for model in PROFILES:
            with self.subTest(model=model):
                connection = SimulatedPort(model)
                session = self.connect(connection)
                self.assertEqual(session.model, model)
                self.assertEqual(session.firmware, "1.23")
                self.assertEqual(session.baud_rate, PROFILES[model].default_baud)
                state = session.read_state()
                self.assertEqual(state["kl"], 1)
                self.assertEqual(state["tg"], 0)
                self.assertEqual(state["tl"], -8)
                self.assertNotIn(b"#tg1\r", connection.writes)
                self.assertNotIn(b"#tg0\r", connection.writes)
                self.assertNotIn(b"#lv\r", connection.writes)

    def test_generator_write_is_followed_by_real_readback(self):
        cases = (
            ("HM5014-2", 0, -25.0, b"#tl-25.0\r", "-25.2", -25.2),
            ("HM5530", 2, -10.0, b"#tl+97.0\r", "+97.2", -9.8),
        )
        for model, unit, requested, command, reported, actual in cases:
            with self.subTest(model=model):
                connection = SimulatedPort(model, level_unit=unit)
                session = self.connect(connection)
                connection.override_test_level = reported
                self.assertAlmostEqual(session.set_value("tl", requested), actual)
                self.assertEqual(connection.writes[-2:], [command, b"#tl\r"])
                self.assertEqual(session.set_value("tg", 1), 1)
                self.assertEqual(connection.writes[-2:], [b"#tg1\r", b"#tg\r"])
                self.assertEqual(session.set_value("tg", 0), 0)
                self.assertEqual(connection.writes[-2:], [b"#tg0\r", b"#tg\r"])

    def test_hm5014_average_starts_detection_and_can_return_to_live_a(self):
        connection = SimulatedPort("HM5014-2")
        session = self.connect(connection)

        self.assertEqual(session.set_average(True), 3)
        self.assertEqual(connection.writes[-5:], [b"#dm\r", b"#dm1\r", b"#dm\r", b"#vm3\r", b"#vm\r"])
        self.assertEqual(session.set_average(True), 3)
        self.assertEqual(connection.writes.count(b"#dm1\r"), 1)
        self.assertEqual(session.set_average(False), 0)
        self.assertEqual(connection.writes[-4:], [b"#vm0\r", b"#vm\r", b"#dm0\r", b"#dm\r"])

    def test_hm5014_restores_detection_when_device_declines_average_display(self):
        class AverageUnavailablePort(SimulatedPort):
            def reply_to(self, command):
                if command == "#vm3":
                    return b"RD\r"  # Leaves the instrument in its previous view.
                return super().reply_to(command)

        connection = AverageUnavailablePort("HM5014-2")
        session = self.connect(connection)
        self.assertEqual(session.set_average(True), 0)
        self.assertEqual(connection.settings["dm"], "0")

    def test_hm5530_average_only_switches_and_reads_back_the_view(self):
        connection = SimulatedPort("HM5530")
        session = self.connect(connection)
        self.assertEqual(session.set_average(True), 3)
        self.assertEqual(session.set_average(False), 0)
        self.assertEqual(connection.writes[-4:], [b"#vm3\r", b"#vm\r", b"#vm0\r", b"#vm\r"])

    def test_wrong_model_closes_before_any_device_setting_is_changed(self):
        connection = SimulatedPort("HM5530", device_baud=4800)
        with self.assertRaises(ModelMismatch):
            self.connect(connection, model="HM5014-2")
        self.assertEqual(connection.writes, [b"#hm\r"])
        self.assertFalse(connection.is_open)

    def test_close_closes_the_port_when_remote_release_times_out(self):
        connection = SimulatedPort("HM5530")
        session = self.connect(connection)
        connection.ignore_remote_off = True
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.1)):
            with self.assertRaises(ProtocolTimeout):
                session.close()
        self.assertFalse(connection.is_open)

    def test_rejects_remote_lock_acknowledgment_with_local_readback(self):
        connection = SimulatedPort("HM5530")
        connection.ignore_remote_on = True
        with self.assertRaisesRegex(ProtocolError, "did not enter remote"):
            self.connect(connection)
        self.assertFalse(connection.is_open)
        self.assertEqual(connection.writes[-2:], [b"#kl1\r", b"#kl\r"])

    def test_close_releases_remote_and_is_idempotent(self):
        connection = SimulatedPort("HM5014-2")
        session = self.connect(connection)
        session.close()
        session.close()
        self.assertEqual(connection.writes[-2:], [b"#kl0\r", b"#kl\r"])
        self.assertEqual(connection.writes.count(b"#kl0\r"), 1)
        self.assertFalse(connection.is_open)

    def test_remote_off_allows_queries_and_rejects_control_until_reenabled(self):
        connection = SimulatedPort("HM5530")
        session = self.connect(connection)
        self.assertEqual(session.set_value("kl", 0), 0)
        self.assertEqual(session.execute("#tg"), "TG 0")
        with self.assertRaisesRegex(ProtocolError, "Remote control is off"):
            session.set_value("tg", 1)
        self.assertNotIn(b"#tg1\r", connection.writes)
        self.assertEqual(session.set_value("kl", 1), 1)
        self.assertEqual(session.set_value("tg", 1), 1)

    def test_baud_change_verifies_identity_without_acknowledgment(self):
        connection = SimulatedPort("HM5530")
        with patch("hameg_studio.protocol.time.sleep"):
            session = self.connect(connection, fast=True)
        self.assertEqual(session.baud_rate, 115200)
        self.assertEqual(connection.writes[-2:], [b"#br115200\r", b"#hm\r"])

    def test_automatic_baud_probe_precedes_remote_control(self):
        connection = SimulatedPort("HM5530", device_baud=115200)
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.01)):
            session = self.connect(connection)
        self.assertEqual(session.baud_rate, 115200)
        self.assertEqual(connection.writes, [b"#hm\r", b"#hm\r", b"#vn\r", b"#kl1\r", b"#kl\r"])

    def test_segmented_binary_reads_preserve_control_bytes_inside_samples(self):
        samples = b"RD\r" + SAMPLES[3:]
        frame = trace_frame(samples=samples)
        for model, reply in (("HM5014-2", frame), ("HM5530", b"RD\r" + frame)):
            with self.subTest(model=model):
                connection = SimulatedPort(model)
                connection.trace_reply = reply
                session = self.connect(connection)
                trace = session.capture()
                self.assertEqual(trace.samples, tuple(samples))
                self.assertEqual(trace.center_mhz, 500)
                self.assertEqual(connection.writes[-1], b"#bm1\r")

    def test_hm5014_capture_realigns_framing_and_checks_the_binary_body(self):
        samples = b"RD\r\n\x00" + SAMPLES[5:]
        frame = trace_frame(samples=samples)
        cases = {
            "acknowledgment ending in CRLF": b"RD\r\n" + frame,
            "command echo and acknowledgment": b"#bm1\r\nRD\r\n" + frame,
            "late text terminator before binary": b"\n" + frame,
            "unexpected leading byte": b"\xaa" + frame,
            "missing final CR": frame[:-1],
            "LF instead of CR": frame[:-1] + b"\n",
            "zero instead of CR": frame[:-1] + b"\x00",
            "prefix and missing final CR": b"RD\r\n" + frame[:-1],
        }
        for description, response in cases.items():
            with self.subTest(description=description):
                connection = SimulatedPort("HM5014-2")
                connection.trace_reply = response
                session = self.connect(connection)
                self.assertEqual(session.capture(), Trace(tuple(samples), 500))
                self.assertEqual(connection.writes.count(b"#bm1\r"), 1)

    def test_hm5014_still_rejects_bad_checksum_or_missing_center_in_recovery(self):
        frame = trace_frame()
        cases = (
            b"RD\r\n" + frame[:56] + b"\x00" + frame[57:-1] + b"\x00",
            trace_frame(center=bytes(10))[:-1] + b"\x00",
        )
        connection = SimulatedPort("HM5014-2")
        session = self.connect(connection)
        for response in cases:
            with self.subTest(byte_count=len(response)):
                connection.trace_reply = response
                with self.assertRaises(TraceTransferError) as invalid:
                    session.capture()
                self.assertEqual(invalid.exception.response, response)
                self.assertIn(f"Bytes received: {len(response)}", invalid.exception.diagnostic_text())
                self.assertIn("Firmware: 1.23", invalid.exception.diagnostic_text())

        connection.trace_reply = frame
        self.assertEqual(session.capture(), Trace(tuple(SAMPLES), 500))

    def test_hm5530_requires_the_documented_terminator(self):
        connection = SimulatedPort("HM5530")
        connection.trace_reply = trace_frame()[:-1] + b"\x00"
        session = self.connect(connection)
        with self.assertRaises(TraceTransferError):
            session.capture()

    def test_hm5014_single_shot_discards_initial_invalid_block(self):
        connection = SimulatedPort("HM5014-2")
        session = self.connect(connection)
        self.assertEqual(session.set_value("es", 1), 1)
        with patch("hameg_studio.protocol.time.monotonic", return_value=100.0) as now:
            with patch("hameg_studio.protocol.time.sleep") as wait:
                self.assertIsInstance(session.execute("#ss1"), str)
                now.return_value = 100.25
                self.assertEqual(session.execute("#ss1"), Trace(tuple(SAMPLES), 500))
                wait.assert_called_once_with(0.75)

    def test_hm5530_waits_for_single_shot_before_capture(self):
        connection = SimulatedPort("HM5530")
        session = self.connect(connection)
        with patch("hameg_studio.protocol.time.monotonic", return_value=100.0) as now:
            with patch("hameg_studio.protocol.time.sleep") as wait:
                self.assertEqual(session.execute("#ss1"), "RD")
                now.return_value = 100.5
                self.assertEqual(session.capture().center_mhz, 500)
                wait.assert_called_once_with(0.5)

    def test_missing_firmware_and_marker_readbacks_preserve_rf_and_capture(self):
        connection = SimulatedPort("HM5530")
        connection.ignored_queries.update(("#vn", "#df"))
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.001)):
            session = self.connect(connection)
            state = session.read_state()
            self.assertEqual(session.firmware, "")
            self.assertEqual((state["kl"], state["tg"], state["tl"], state["cf"]), (1, 0, -8, 500))
            self.assertNotIn("df", state)
            self.assertEqual(session.capture().center_mhz, 500)
            self.assertNotIn("df", session.read_state())
            self.assertEqual(connection.writes.count(b"#df\r"), 1)
            connection.ignored_queries.remove("#df")
            self.assertEqual(session.read_state(retry_missing=True)["df"], 100)

    def test_unknown_level_unit_never_falls_back_to_dbm(self):
        connection = SimulatedPort("HM5530", level_unit=2)
        connection.ignored_queries.add("#du")
        session = self.connect(connection)
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.001)):
            state = session.read_state()
            self.assertEqual(state["tg"], 0)
            self.assertNotIn("tl", state)
            self.assertNotIn("rl", state)
            with self.assertRaises(ProtocolTimeout):
                session.set_value("tl", -6)
        self.assertFalse(any(command.startswith(b"#tl") for command in connection.writes))

    def test_link_lost_after_lock_reply_has_a_bounded_state_read(self):
        connection = SimulatedPort("HM5530")
        session = self.connect(connection)
        connection.ignored_queries.update(
            f"#{key}" for key in session.profile.query_keys if key != "kl"
        )
        writes_before = len(connection.writes)
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.01)):
            self.assertEqual(session.read_state(), {"kl": 1})
        self.assertLessEqual(len(connection.writes) - writes_before, 4)

    def test_lost_acknowledgment_uses_readback_and_never_replays_setter(self):
        connection = SimulatedPort("HM5014-2")
        session = self.connect(connection)
        connection.lost_acknowledgments.add("#tg1")
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.001)):
            self.assertEqual(session.set_value("tg", 1), 1)
        self.assertEqual(connection.writes[-2:], [b"#tg1\r", b"#tg\r"])
        self.assertEqual(connection.writes.count(b"#tg1\r"), 1)

    def test_fast_link_falls_back_when_baud_change_is_ignored(self):
        connection = SimulatedPort("HM5530")
        connection.accept_baud_change = False
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.001)):
            with patch("hameg_studio.protocol.time.sleep"):
                session = self.connect(connection, fast=True)
        self.assertEqual(session.baud_rate, 9600)
        self.assertEqual(session.read_state()["kl"], 1)

    def test_unidentified_port_gets_only_read_queries_and_closes(self):
        connection = SimulatedPort("HM5014-2")
        connection.ignored_queries.add("#hm")
        with patch("hameg_studio.protocol.time.monotonic", side_effect=count(step=0.001)):
            with self.assertRaisesRegex(ProtocolTimeout, r"No identity response from COM42"):
                self.connect(connection)
        self.assertTrue(connection.writes)
        self.assertEqual(set(connection.writes), {b"#hm\r"})
        self.assertFalse(connection.is_open)


if __name__ == "__main__":
    unittest.main()
