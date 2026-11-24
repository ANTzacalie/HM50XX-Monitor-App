import math
import random

from .protocol import Trace


AVERAGE_RESET_SETTINGS = {"cf", "sp", "sr", "st", "rl", "at", "db", "bw", "vf", "tg", "tl", "vm", "dm"}


class DemoAnalyzer:
    """In-memory preview. Nothing in this class opens a COM port."""

    firmware = ""

    def __init__(self, profile):
        self.profile = profile
        self.model = profile.model
        self.baud_rate = profile.default_baud
        self.sweep = 0
        self.memory = None
        self.latest = None
        self._average: list[float] | None = None
        self.values = {
            "cf": 100.0, "sp": 100, "rl": -15.0, "at": 10, "bw": 120,
            "db": 10, "tg": 0, "tl": -10.0, "vf": 0, "vm": 0, "dm": 0,
            "kl": 1, "uc": 0, "sr": 50.0, "st": 150.0, "du": 0,
            "ra": 0, "ba": 0, "mf": 100.0, "df": 0.0, "mk": 0,
            "lv": -28.0, "es": 0, "et": 0,
        }
        self.slots = {}

    def read_state(self, *, retry_missing=False) -> dict:
        return {
            key: self.values[key] for key, setting in self.profile.settings.items()
            if key in self.values and setting.readable
        }

    def set_value(self, key, value):
        self.profile.setting_command(key, value)
        setting = self.profile.settings[key]
        value = float(str(value).replace(",", ".")) if setting.decimals else int(value)
        if key == "sv":
            self.slots[value] = self.values.copy()
        elif key == "rc" and value in self.slots:
            self.values.update(self.slots[value])
            self._average = None
        if key in AVERAGE_RESET_SETTINGS and value != self.values.get(key):
            self._average = None
        self.values[key] = value
        if key in ("sr", "st"):
            self.values["sp"] = max(0.0, self.values["st"] - self.values["sr"])
            self.values["cf"] = (self.values["sr"] + self.values["st"]) / 2
        if key in ("cf", "sp"):
            self.values["sr"] = self.values["cf"] - self.values["sp"] / 2
            self.values["st"] = self.values["cf"] + self.values["sp"] / 2
        return value

    def set_average(self, enabled: bool) -> int:
        """Select the model's instrument-style averaging mode in Demo."""
        has_detector = "dm" in self.profile.settings
        if enabled and has_detector:
            self.set_value("dm", 1)
        mode = self.set_value("vm", 3 if enabled else 0)
        if not enabled and has_detector:
            self.set_value("dm", 0)
        return mode

    def execute(self, command: str):
        command = command.strip().lower()
        if command in ("#bm1", "#ss1"):
            return self.capture()
        if command == "#sa":
            self.memory = self.latest or self.capture()
            return "RD"
        if command == "#hm":
            return self.model.removeprefix("HM")
        if command == "#vn":
            return "DEMO"
        key = command[1:3]
        if len(command) == 3 and key in self.read_state():
            return f"{key.upper()}{self.values[key]}"
        if command.startswith("#") and key in self.profile.settings and len(command) > 3:
            self.set_value(key, command[3:])
            return "RD"
        raise ValueError("Unknown command or unavailable in simulation")

    def capture(self) -> Trace:
        self.sweep += 1
        randomizer = random.Random(self.sweep)
        center, span = self.values["cf"], self.values["sp"]
        reference, scale = self.values["rl"], self.values["db"]
        generator_on = self.values["tg"] == 1
        output_level = self.values["tl"]
        peaks = ((65, 24, 1.9), (84, 40, 1.6), (102, 55, 1.2), (121, 21, 2.2), (132, 32, 1.6))
        if generator_on and self.model == "HM5530":
            peaks = ((50, 74 + output_level, 1.5), (100, 54 + output_level, 2.5),
                     (150, 30 + output_level, 2.1), *peaks)

        samples = []
        for index in range(self.profile.sample_count):
            fraction = index / (self.profile.sample_count - 1)
            frequency = center + span * (fraction - 0.5)
            floor = -92 + randomizer.uniform(-3.2, 3.2) + 1.8 * math.sin(fraction * 23)
            if not span:
                level = floor + 40 * (0.5 + 0.5 * math.sin(fraction * 24)) ** 3
            elif generator_on and self.model == "HM5014-2":
                level = output_level - 16 - 26 / (1 + ((frequency - 106) / 5) ** 2)
                level += randomizer.uniform(-0.5, 0.5)
            else:
                level = floor + max(height / (1 + ((frequency - peak) / width) ** 2)
                                    for peak, height, width in peaks)
            samples.append(max(0, min(255, round(229 + (level - reference) * 25 / scale))))

        if self.values["vm"] == 3:
            # Keep the previous average at full precision so small changes
            # carry forward even when the rendered ADC count rounds down.
            if self._average is None:
                self._average = [float(sample) for sample in samples]
            else:
                self._average = [
                    (previous + sample) / 2
                    for previous, sample in zip(self._average, samples, strict=True)
                ]
            samples = [round(sample) for sample in self._average]

        self.latest = Trace(samples=tuple(samples), center_mhz=center)
        if self.memory is not None:
            if self.values["vm"] == 1:
                return self.memory
            if self.values["vm"] == 2:
                difference = tuple(max(0, min(255, a - b + 129))
                                   for a, b in zip(samples, self.memory.samples))
                return Trace(samples=difference, center_mhz=center)
        return self.latest

    def close(self, release_remote=True) -> None:
        pass
