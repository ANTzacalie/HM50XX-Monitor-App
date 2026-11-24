import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import APP_NAME


SETTINGS_ROOT = Path(os.environ.get("LOCALAPPDATA", Path.home()))
SETTINGS_FILE = SETTINGS_ROOT / APP_NAME / "settings.json"
LEGACY_SETTINGS_FILE = SETTINGS_ROOT / "HAMEG Spectrum Studio" / "settings.json"
TRACE_FOCUS_LEVELS = ("normal", "sharp", "soft")
TRACE_INTENSITY_LEVELS = (100, 70, 40)


@dataclass
class Preferences:
    language: str = "en"
    theme: str = "dark"
    model: str = "HM5014-2"
    live_on_connect: bool = True
    stream_interval_ms: int = 250
    fast_link: bool = False
    ports: dict[str, str] = field(default_factory=dict)
    baud_rates: dict[str, int] = field(default_factory=dict)
    trace_focus: str = "normal"
    trace_intensity: int = 100

    @classmethod
    def load(cls, path: Path = SETTINGS_FILE) -> "Preferences":
        if path == SETTINGS_FILE and not path.exists():
            path = LEGACY_SETTINGS_FILE
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return cls()
        preferences = cls()
        if not isinstance(values, dict):
            return preferences
        for key, options in {
            "language": ("en", "ro"),
            "theme": ("dark", "light"),
            "model": ("HM5014-2", "HM5530"),
            "stream_interval_ms": (100, 250, 500, 1000, 2000),
            "live_on_connect": (True, False),
            "fast_link": (True, False),
            "trace_focus": TRACE_FOCUS_LEVELS,
            "trace_intensity": TRACE_INTENSITY_LEVELS,
        }.items():
            value = values.get(key)
            if type(value) is type(getattr(preferences, key)) and value in options:
                setattr(preferences, key, value)
        for key, expected_type in (("ports", str), ("baud_rates", int)):
            if isinstance(values.get(key), dict):
                setattr(preferences, key, {
                    model: value for model, value in values[key].items()
                    if type(value) is expected_type
                })
        return preferences

    def save(self, path: Path = SETTINGS_FILE) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", suffix=".tmp",
                                    dir=path.parent, delete=False) as file:
                temporary = Path(file.name)
                json.dump(asdict(self), file, indent=2)
            temporary.replace(path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
