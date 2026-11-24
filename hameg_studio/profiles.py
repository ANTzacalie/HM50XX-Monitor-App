"""Model-specific limits and ASCII command formats from the HAMEG manuals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


# At 50 ohms, 0 dBm is 46.9897 dBmV or 106.9897 dBµV. The HM5530
# represents these offsets with one decimal place on its serial interface.
LEVEL_UNIT_OFFSETS = (Decimal("0.0"), Decimal("47.0"), Decimal("107.0"))
GENERATOR_STEP = Decimal("0.2")


@dataclass(frozen=True)
class Setting:
    """One numeric instrument parameter, expressed in the application's units."""

    options: tuple[int, ...] = ()
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    step: Decimal | None = None
    unit: str = ""
    decimals: int = 0
    readable: bool = True
    writable: bool = True
    minimum_nonzero: Decimal | None = None

    def validate(self, value: int | float | str | Decimal) -> Decimal:
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation as error:
            raise ValueError("Enter a numeric value.") from error

        if not number.is_finite():
            raise ValueError("Enter a finite value.")
        if self.options and number not in self.options:
            choices = ", ".join(str(option) for option in self.options)
            raise ValueError(f"Choose one of {choices}.")
        if self.minimum is not None and self.maximum is not None:
            if not self.minimum <= number <= self.maximum:
                raise ValueError(f"Enter a value from {self.minimum} to {self.maximum}.")
        if self.minimum_nonzero is not None and 0 < number < self.minimum_nonzero:
            raise ValueError(f"Enter zero or a value of at least {self.minimum_nonzero}.")
        if self.step is not None and number % self.step:
            raise ValueError(f"Enter a multiple of {self.step}.")
        return abs(number) if number.is_zero() else number


@dataclass(frozen=True)
class Profile:
    model: str
    minimum_mhz: float
    maximum_mhz: int
    default_baud: int
    baud_rates: tuple[int, ...]
    generator_name: str
    generator_min: int
    generator_max: int
    settings: dict[str, Setting]
    actions: dict[str, str]
    generator_step: Decimal = GENERATOR_STEP
    sample_count: int = 2001

    @property
    def query_keys(self) -> tuple[str, ...]:
        """Return setting queries supported by this model, in declaration order."""
        return tuple(key for key, setting in self.settings.items() if setting.readable)

    def setting_command(
        self,
        key: str,
        value: int | float | str | Decimal,
        *,
        level_unit: int = 0,
    ) -> str:
        """Format a setter without its CR terminator. Absolute levels use dBm."""
        setting = self.settings.get(key)
        if setting is None or not setting.writable:
            raise ValueError(f"{self.model} does not support setting {key.upper()}.")

        number = setting.validate(value)
        if key in {"rl", "tl"}:
            number += self.level_offset(level_unit)
            parameter = f"{number:+05.1f}"
        elif setting.unit == "MHz" and setting.decimals == 3:
            magnitude = f"{abs(number):08.3f}"
            parameter = f"-{magnitude}" if number < 0 else magnitude
        else:
            parameter = f"{number:.{setting.decimals}f}"
        return f"#{key}{parameter}"

    def parse_reply(self, key: str, reply: str, *, level_unit: int = 0) -> int | float:
        """Parse a query response. Absolute levels are normalized to dBm."""
        setting = self.settings.get(key)
        if setting is None or not setting.readable:
            raise ValueError(f"{self.model} does not support reading {key.upper()}.")

        expected_prefix = "ML|DL" if key == "lv" else key.upper()
        match = re.fullmatch(
            rf"({expected_prefix})\s*([+-]?[0-9]+(?:\.[0-9]+)?)", reply.strip().upper()
        )
        if match is None:
            raise ValueError(f"Unexpected {key.upper()} response: {reply!r}.")

        prefix, parameter = match.groups()
        number = Decimal(parameter)
        if key in {"rl", "tl"} or prefix == "ML":
            number -= self.level_offset(level_unit)
        if setting.options and number not in setting.options:
            raise ValueError(f"Unexpected {key.upper()} response: {reply!r}.")
        if setting.decimals:
            return float(number)
        if number != number.to_integral_value():
            raise ValueError(f"Expected an integer in {key.upper()} response: {reply!r}.")
        return int(number)

    def action_command(self, key: str) -> str:
        try:
            return self.actions[key]
        except KeyError as error:
            raise ValueError(f"{self.model} does not support {key!r}.") from error

    def level_offset(self, level_unit: int) -> Decimal:
        """Return the selected instrument-unit offset relative to dBm."""
        if level_unit == 0:
            return LEVEL_UNIT_OFFSETS[0]
        if "du" not in self.settings or level_unit not in (1, 2):
            raise ValueError(f"{self.model} does not support level unit {level_unit!r}.")
        return LEVEL_UNIT_OFFSETS[level_unit]


def frequency(
    maximum: int, *, minimum: int = 0, minimum_nonzero: int | None = None
) -> Setting:
    return Setting(
        minimum=Decimal(minimum),
        maximum=Decimal(maximum),
        step=Decimal("0.001"),
        unit="MHz",
        decimals=3,
        minimum_nonzero=Decimal(minimum_nonzero) if minimum_nonzero else None,
    )


def level(minimum: str, maximum: str, step: str) -> Setting:
    return Setting(
        minimum=Decimal(minimum),
        maximum=Decimal(maximum),
        step=Decimal(step),
        unit="dBm",
        decimals=1,
    )


def actions() -> dict[str, str]:
    return {"copy_trace": "#sa", "single_sweep": "#ss1"}


HM5014_BAUD_RATES = (4800, 9600, 38400, 115200)
HM5530_BAUD_RATES = (4800, 9600, 19200, 38400, 115200)
ON_OFF = Setting(options=(0, 1))
RBW = Setting(options=(9, 120, 1000), unit="kHz")
SCALE = Setting(options=(5, 10), unit="dB/div")
TRACE_MODE = Setting(options=(0, 1, 2, 3, 4))
EEPROM_SLOT = Setting(options=tuple(range(10)), readable=False)


PROFILES = {
    "HM5014-2": Profile(
        model="HM5014-2",
        minimum_mhz=0.15,
        maximum_mhz=1050,
        default_baud=4800,
        baud_rates=HM5014_BAUD_RATES,
        generator_name="tracking_generator",
        generator_min=-50,
        generator_max=1,
        settings={
            "cf": frequency(1050),
            "sp": Setting(options=(0, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000), unit="MHz"),
            "rl": level("-99.6", "10.0", "0.2"),
            "at": Setting(options=(0, 10, 20, 30, 40), unit="dB"),
            "db": SCALE,
            "bw": RBW,
            "vf": ON_OFF,
            "vm": TRACE_MODE,
            "dm": ON_OFF,
            "tg": ON_OFF,
            "tl": level("-50.0", "1.0", "0.2"),
            "kl": ON_OFF,
            "uc": Setting(options=(0, 1), writable=False),
            "es": Setting(options=(0, 1), readable=False),
            "br": Setting(options=HM5014_BAUD_RATES, readable=False),
            "sv": EEPROM_SLOT,
            "rc": EEPROM_SLOT,
        },
        actions=actions(),
    ),
    "HM5530": Profile(
        model="HM5530",
        minimum_mhz=0.1,
        maximum_mhz=3000,
        default_baud=9600,
        baud_rates=HM5530_BAUD_RATES,
        generator_name="test_source",
        generator_min=-10,
        generator_max=0,
        settings={
            "du": Setting(options=(0, 1, 2)),
            "cf": frequency(3000),
            "sp": frequency(3000, minimum_nonzero=1),
            "sr": frequency(3000),
            "st": frequency(3000),
            "rl": level("-110.0", "20.0", "0.1"),
            "ra": ON_OFF,
            "at": Setting(options=(0, 10, 20, 30, 40, 50), unit="dB"),
            "db": SCALE,
            "bw": RBW,
            "ba": ON_OFF,
            "vf": ON_OFF,
            "vm": TRACE_MODE,
            "mk": Setting(options=(0, 1, 2)),
            "mf": frequency(3000),
            "df": frequency(3000, minimum=-3000),
            "lv": Setting(unit="dB", decimals=1, writable=False),
            "tg": ON_OFF,
            "tl": level("-10.0", "0.0", "0.2"),
            "kl": ON_OFF,
            "uc": Setting(options=(0, 1), writable=False),
            "es": Setting(options=(0, 1), readable=False),
            "et": Setting(options=(0, 1), readable=False),
            "br": Setting(options=HM5530_BAUD_RATES, readable=False),
        },
        actions=actions(),
    ),
}
