"""Synchronous HAMEG serial I/O. Only the connection worker owns the port."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal

import serial

from .profiles import Profile


FRAME_SIZE = 2048
FRAME_BODY_SIZE = FRAME_SIZE - 1
SAMPLE_COUNT = 2001
MAX_TRACE_PREFIX = 64
CENTER_FIELD = slice(2016, 2026)
CHECKSUM_FIELD = slice(2044, 2047)
TERMINATOR = b"\r"
CENTER_PATTERN = re.compile(rb"CF[0-9]{4}\.[0-9]{3}", re.IGNORECASE)
TRACE_PREFIX_PATTERN = re.compile(rb"(?:[\r\n]|(?:RD|#BM1|#SS1)[\r\n])+", re.IGNORECASE)
IDENTITY_PATTERN = re.compile(r"(?:HAMEG\s*)?(?:HM\s*)?(\d{4}(?:-\d+)?)", re.ASCII | re.IGNORECASE)
FIRMWARE_PATTERN = re.compile(r"(?:VN\s*)?(\d+\.\d+)", re.ASCII | re.IGNORECASE)
COMMAND_PATTERN = re.compile(r"#([a-z]{2})([+-]?\d+(?:\.\d+)?)?", re.ASCII | re.IGNORECASE)
LEVEL_KEYS = {"rl", "tl", "lv"}
PRIORITY_QUERIES = ("du", "tg", "tl", "cf", "sp", "rl", "db", "vm")
STATE_READ_BUDGET_SECONDS = 3.0
MISSING_READBACK_RETRY_SECONDS = 10.0
SINGLE_SHOT_SECONDS = 1.0
Number = int | float


class ProtocolError(RuntimeError):
    """A device or its serial response could not complete the operation."""


class ProtocolTimeout(ProtocolError):
    """The instrument did not finish its response within the allowed time."""


class ModelMismatch(ProtocolError):
    """The selected serial port responded with a different instrument model."""


class TraceTransferError(ProtocolError):
    """A bounded copy of the wire response for local troubleshooting."""

    def __init__(self, command: str, model: str, firmware: str, baud: int, response: bytes):
        super().__init__(f"{command.upper()}: {len(response)} bytes received, no verified spectrum block.")
        self.command = command.upper()
        self.model = model
        self.firmware = firmware
        self.baud = baud
        self.response = response

    def diagnostic_text(self) -> str:
        lines = [
            f"Model: {self.model}",
            f"Firmware: {self.firmware or 'unknown'}",
            f"Baud: {self.baud}",
            f"Command: {self.command}",
            f"Bytes received: {len(self.response)}",
            "",
        ]
        for offset in range(0, len(self.response), 16):
            lines.append(f"{offset:04X}  {self.response[offset:offset + 16].hex(' ').upper()}")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Trace:
    """The ADC sample bytes and center frequency returned by the instrument."""

    samples: tuple[int, ...]
    center_mhz: float | None


def parse_trace(frame: bytes) -> Trace:
    """Decode the fixed-width #BM1 data block from either instrument."""
    if len(frame) != FRAME_SIZE or frame[-1:] != TERMINATOR:
        raise ProtocolError("Invalid spectrum block length or terminator.")
    return _parse_trace_body(frame)


def _parse_trace_body(frame: bytes, *, require_center: bool = False) -> Trace:
    trace_bytes = frame[:SAMPLE_COUNT]
    received_checksum = int.from_bytes(frame[CHECKSUM_FIELD], "big")
    if sum(trace_bytes) != received_checksum:
        raise ProtocolError("Spectrum block checksum mismatch.")

    # An empty metadata field leaves the client free to use a separately read center.
    center_field = frame[CENTER_FIELD]
    if center_field == bytes(10) and not require_center:
        center_mhz = None
    elif CENTER_PATTERN.fullmatch(center_field):
        center_mhz = float(center_field[2:])
    else:
        raise ProtocolError("Malformed center frequency in spectrum block.")
    return Trace(tuple(trace_bytes), center_mhz)


def _find_trace(response: bytearray, *, missing_terminator: bool = False) -> Trace | None:
    size = FRAME_BODY_SIZE if missing_terminator else FRAME_SIZE
    last_offset = min(MAX_TRACE_PREFIX, len(response) - size)
    for offset in range(last_offset + 1):
        if not missing_terminator and response[offset + FRAME_BODY_SIZE] != 0x0D:
            continue
        frame = bytes(response[offset:offset + size])
        # Without an explicit reply prefix, require the fixed CF field to
        # establish alignment. Require it whenever the terminator is unusable.
        unknown_prefix = offset > 0 and not TRACE_PREFIX_PATTERN.fullmatch(response[:offset])
        with suppress(ProtocolError):
            return _parse_trace_body(frame, require_center=missing_terminator or unknown_prefix)
    return None


class SerialAnalyzer:
    """An identified, exclusive session with one analyzer on a COM port."""

    def __init__(self, profile: Profile, connection: serial.Serial) -> None:
        self.profile = profile
        self._port = connection
        self._remote = False
        self._single_shot_started = False
        self._single_shot_ready_at = 0.0
        self._readback_retry_at: dict[str, float] = {}
        self.model = profile.model
        self.firmware = ""

    @property
    def baud_rate(self) -> int:
        return self._port.baudrate

    @classmethod
    def connect(
        cls, profile: Profile, port: str, baud: int | None = None, fast: bool = False
    ) -> SerialAnalyzer:
        """Find the current baud rate, verify identity, and enable remote control."""
        if baud is not None and baud not in profile.baud_rates:
            raise ValueError(f"Unsupported baud rate for {profile.model}: {baud}")
        if baud is None:
            other_rates = sorted(set(profile.baud_rates) - {profile.default_baud}, reverse=True)
            candidate_rates = (profile.default_baud, *other_rates)
        else:
            candidate_rates = (baud,)

        connection = serial.Serial(
            port=port,
            baudrate=candidate_rates[0],
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
            write_timeout=0.8,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        analyzer = cls(profile, connection)
        try:
            for rate in candidate_rates:
                if analyzer._probe(rate):
                    break
            else:
                tried = ", ".join(map(str, candidate_rates))
                raise ProtocolTimeout(f"No identity response from {port} at {tried} baud.")

            with suppress(ProtocolTimeout):
                analyzer.firmware = analyzer._read_firmware()
            # The device may apply the lock even if the acknowledgment is lost.
            analyzer._remote = True
            analyzer.set_value("kl", 1)
            if not analyzer._remote:
                raise ProtocolError("The instrument did not enter remote control mode.")
            if fast:
                analyzer._change_baud(max(profile.baud_rates), allow_fallback=True)
            return analyzer
        except BaseException:
            with suppress(ProtocolError, OSError):
                analyzer.close()
            raise

    def read_state(self, *, retry_missing: bool = False) -> dict[str, Number]:
        """Return confirmed values. A missing readback leaves that field unknown."""
        if retry_missing:
            self._readback_retry_at.clear()
        # Verify the link and lock first. A dead connection should fail once,
        # instead of waiting through a timeout for every setting.
        state = {"kl": self._read_value("kl")}
        deadline = time.monotonic() + STATE_READ_BUDGET_SECONDS
        queries = (*PRIORITY_QUERIES, *self.profile.query_keys)
        for key in dict.fromkeys(queries):
            if key in {"kl", "lv"} or key not in self.profile.settings:
                continue
            self._read_state_value(state, key, deadline)
        if "lv" in self.profile.settings and state.get("mk") in (1, 2):
            self._read_state_value(state, "lv", deadline)
        return state

    def _read_state_value(self, state: dict[str, Number], key: str, deadline: float) -> None:
        now = time.monotonic()
        if now >= deadline or now < self._readback_retry_at.get(key, 0):
            return
        if key in LEVEL_KEYS and "du" in self.profile.settings and "du" not in state:
            return  # An unknown instrument unit cannot be converted to dBm.
        try:
            state[key] = self._read_value(key, int(state.get("du", 0)), min(1.2, deadline - now))
        except ProtocolError:
            self._readback_retry_at[key] = time.monotonic() + MISSING_READBACK_RETRY_SECONDS

    def set_value(self, key: str, value: Number | str | Decimal) -> Number:
        """Apply one value and return the actual readback when the model offers it."""
        self._require_remote(key)
        level_unit = self._read_level_unit(key)
        command = self.profile.setting_command(key, value, level_unit=level_unit)
        if key == "br":
            self._change_baud(int(command[3:]))
            return self.baud_rate

        setting = self.profile.settings[key]
        try:
            self._acknowledge(command)
        except ProtocolTimeout:
            if not setting.readable:
                raise
            # Never replay a setter. The instrument may have applied it before
            # the acknowledgment was lost, so query the device to confirm it.
            actual = self._read_value(key, level_unit)
            if Decimal(str(actual)) != setting.validate(value):
                raise
            return actual
        if key == "es":
            self._single_shot_started = False
            self._single_shot_ready_at = 0.0
        if setting.readable:
            return self._read_value(key, level_unit)
        return int(command[3:])

    def set_average(self, enabled: bool) -> int:
        """Select the analyzer's averaged trace or return to live Signal A."""
        self._require_remote("vm")
        separate_detection = "dm" in self.profile.settings
        started_detection = enabled and separate_detection and self._read_value("dm") == 0
        if started_detection and self.set_value("dm", 1) != 1:
            raise ProtocolError("The instrument did not enable Average detection.")

        mode = int(self.set_value("vm", 3 if enabled else 0))
        if separate_detection:
            # Restore detection if this request enabled it but the analyzer
            # declined the averaged display. A confirmed return to A ends AVG.
            if (started_detection and mode != 3) or (not enabled and mode == 0):
                if self.set_value("dm", 0) != 0:
                    raise ProtocolError("The instrument did not stop Average detection.")
        return mode

    def capture(self) -> Trace:
        """Read one complete, checksummed spectrum block."""
        self._require_remote("bm")
        self._wait_for_single_shot()
        return self._capture_command("#bm1")

    def execute(self, command: str) -> str | Trace:
        """Execute one documented command from the manual controls."""
        command = command.strip().lower()
        match = COMMAND_PATTERN.fullmatch(command)
        if match is None:
            raise ValueError("Enter one instrument command, for example #cf or #vm2.")
        key, argument = match.groups()
        if key in self.profile.settings:
            setting = self.profile.settings[key]
            if argument is None and setting.readable:
                return self._query_setting(key)
            if argument is not None and setting.writable:
                if key in LEVEL_KEYS:
                    unit = self._read_level_unit(key)
                    value = self.profile.parse_reply(key, key + argument, level_unit=unit)
                else:
                    value = float(argument) if "." in argument else int(argument)
                actual = self.set_value(key, value)
                return f"BR{actual}" if key == "br" else "RD"

        if command == "#hm":
            return self._read_identity()
        if command == "#vn":
            return self._read_firmware()
        if command == "#bm1":
            return self.capture()
        if command in self.profile.actions.values():
            return self._execute_action(command)
        raise ValueError(f"Unsupported command for {self.model}: {command}")

    def close(self, release_remote: bool = True) -> None:
        """Release front-panel control and close the serial port."""
        if not self._port.is_open:
            return
        try:
            if release_remote and self._remote:
                if self.set_value("kl", 0) != 0:
                    raise ProtocolError("The instrument still reports remote control enabled.")
        finally:
            self._remote = False
            self._port.close()

    def _probe(self, baud: int) -> bool:
        if baud != self.baud_rate:
            self._port.baudrate = baud
        try:
            found_model = self._read_identity(timeout=1.3)
        except ProtocolTimeout:
            return False
        if found_model != self.model:
            raise ModelMismatch(f"Connected instrument is {found_model}. Selected model is {self.model}.")
        return True

    def _change_baud(self, baud: int, *, allow_fallback: bool = False) -> None:
        if baud not in self.profile.baud_rates:
            raise ValueError(f"Unsupported baud rate for {self.model}: {baud}")
        previous_baud = self.baud_rate
        if baud == previous_baud:
            return

        # The 5530 explicitly sends no RD for #BR. Verify the new line rate by
        # identity instead of relying on an acknowledgment at either rate.
        self._send(f"#br{baud}")
        deadline = time.monotonic() + 0.8
        while self._port.out_waiting:
            if time.monotonic() >= deadline:
                raise ProtocolTimeout("The adapter did not finish sending the baud change.")
            time.sleep(0.01)
        time.sleep(0.12)
        if self._probe(baud) or self._probe(baud):
            return
        if self._probe(previous_baud):
            if allow_fallback:
                return
            raise ProtocolError(f"The instrument stayed at {previous_baud} baud.")
        raise ProtocolTimeout(f"The instrument stopped responding after changing to {baud} baud.")

    def _read_identity(self, timeout: float = 1.2) -> str:
        response = self._exchange_line("#hm", lambda reply: bool(IDENTITY_PATTERN.fullmatch(reply)), timeout)
        match = IDENTITY_PATTERN.fullmatch(response)
        assert match is not None
        return "HM" + match[1]

    def _read_firmware(self) -> str:
        response = self._exchange_line("#vn", lambda reply: bool(FIRMWARE_PATTERN.fullmatch(reply)))
        match = FIRMWARE_PATTERN.fullmatch(response)
        assert match is not None
        return match[1]

    def _read_level_unit(self, key: str) -> int:
        if key in LEVEL_KEYS and "du" in self.profile.settings:
            return int(self._read_value("du"))
        return 0

    def _read_value(self, key: str, level_unit: int = 0, timeout: float = 1.2) -> Number:
        reply = self._query_setting(key, timeout)
        try:
            value = self.profile.parse_reply(key, reply, level_unit=level_unit)
        except ValueError as error:
            raise ProtocolError(f"Invalid #{key} response: {reply!r}") from error
        if key == "kl":
            self._remote = bool(value)
        self._readback_retry_at.pop(key, None)
        return value

    def _query_setting(self, key: str, timeout: float = 1.2) -> str:
        prefixes = ("ML", "DL") if key == "lv" else (key.upper(),)
        return self._exchange_line(f"#{key}", lambda reply: reply.upper().startswith(prefixes), timeout)

    def _require_remote(self, key: str) -> None:
        if not self._remote and key != "kl":
            raise ProtocolError("Remote control is off. Send #kl1 to regain control.")

    def _execute_action(self, command: str) -> str | Trace:
        self._require_remote(command[1:3])
        if command == "#ss1":
            self._wait_for_single_shot()
        if command == "#ss1" and self.model == "HM5014-2":
            if not self._single_shot_started:
                try:
                    self._capture_command(command)
                except TraceTransferError as error:
                    # The first response belongs to a measurement that has
                    # not finished. Its data are documented as invalid.
                    if len(error.response) < FRAME_BODY_SIZE:
                        raise
                self._single_shot_started = True
                return "Sweep started. The next #ss1 returns its data."
            return self._capture_command(command)

        self._acknowledge(command, timeout=2.5 if command == "#ss1" else 1.2)
        return "RD"

    def _wait_for_single_shot(self) -> None:
        remaining = self._single_shot_ready_at - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)

    def _acknowledge(self, command: str, timeout: float = 1.2) -> None:
        self._exchange_line(command, lambda reply: reply.upper() == "RD", timeout)

    def _capture_command(self, command: str) -> Trace:
        self._send(command)
        limit = FRAME_SIZE + MAX_TRACE_PREFIX
        deadline = time.monotonic() + limit * 10 / self.baud_rate + 2.0
        response = bytearray()
        while len(response) < limit and time.monotonic() < deadline:
            # Read the nominal block first, then allow room for framing bytes
            # before it. CR and RD inside the binary samples are ordinary data.
            target = FRAME_SIZE if len(response) < FRAME_SIZE else limit
            chunk = self._port.read(target - len(response))
            if not chunk:
                if len(response) >= FRAME_BODY_SIZE:
                    break
                continue
            response.extend(chunk)
            trace = _find_trace(response)
            if trace is not None:
                return trace

        if self.model == "HM5014-2":
            # A complete, checksum-verified body with the fixed CF field can
            # identify a sweep even if its final delimiter is absent or wrong.
            trace = _find_trace(response, missing_terminator=True)
            if trace is not None:
                return trace
        raise TraceTransferError(command, self.model, self.firmware, self.baud_rate, bytes(response))

    def _exchange_line(
        self, command: str, accepts: Callable[[str], bool], timeout: float = 1.2
    ) -> str:
        self._send(command)
        deadline = time.monotonic() + timeout
        line = bytearray()
        while time.monotonic() < deadline:
            byte = self._port.read(1)
            if byte in {TERMINATOR, b"\n"}:
                reply = line.decode("ascii", errors="replace").strip()
                if accepts(reply):
                    return reply
                line.clear()
            elif byte and len(line) < 256:
                line.extend(byte)
        raise ProtocolTimeout(f"No expected reply to {command} at {self.baud_rate} baud.")

    def _send(self, command: str) -> None:
        self._port.reset_input_buffer()
        payload = command.encode("ascii") + TERMINATOR
        if self._port.write(payload) != len(payload):
            raise ProtocolError(f"The serial port did not send all bytes of {command}.")
        if command == "#ss1":
            self._single_shot_ready_at = time.monotonic() + SINGLE_SHOT_SECONDS
