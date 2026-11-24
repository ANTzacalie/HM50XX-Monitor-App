import tkinter as tk

from .preferences import TRACE_FOCUS_LEVELS, TRACE_INTENSITY_LEVELS
from .spectrum import (
    COUNTS_PER_DIVISION, HORIZONTAL_DIVISIONS, TOP_COUNT, VERTICAL_DIVISIONS, Spectrum,
)


TRACE_WIDTHS = {"normal": 1.4, "sharp": 1.0, "soft": 2.8}


def blend_trace_color(color: str, background: str, intensity: int) -> str:
    """Interpolate a trace palette color toward the plot background."""
    weight = intensity / 100
    foreground_channels = bytes.fromhex(color[1:])
    background_channels = bytes.fromhex(background[1:])
    channels = bytes(
        round(base + (ink - base) * weight)
        for ink, base in zip(foreground_channels, background_channels, strict=True)
    )
    return "#" + channels.hex()


class SpectrumDisplay(tk.Canvas):
    """Draw the instrument's ten-by-eight graticule from a sweep snapshot."""

    def __init__(self, parent, colors, translate, marker_callback):
        super().__init__(parent, background=colors["screen"], highlightthickness=0,
                         borderwidth=0, cursor="crosshair", takefocus=True)
        self.colors = colors
        self.translate = translate
        self.marker_callback = marker_callback
        self.spectrum: Spectrum | None = None
        self.marker: int | None = None
        self.bounds = (0, 0, 1, 1)
        self.empty_hint = "trace_hint"
        self.trace_focus = "normal"
        self.trace_intensity = 100
        self.mono = ("Consolas", 10)
        self.bind("<Configure>", lambda _event: self.draw())
        self.bind("<Button-1>", self.place_marker)
        self.bind("<Left>", lambda _event: self.move_marker(-1))
        self.bind("<Right>", lambda _event: self.move_marker(1))
        self.bind("<Escape>", lambda _event: self.select_marker(None))

    def show(self, spectrum, empty_hint="trace_hint"):
        self.spectrum = spectrum
        self.empty_hint = empty_hint
        self.draw()

    def set_appearance(self, focus: str, intensity: int) -> None:
        """Adjust the local trace stroke and contrast."""
        if focus not in TRACE_FOCUS_LEVELS or intensity not in TRACE_INTENSITY_LEVELS:
            raise ValueError("Invalid trace focus or intensity.")
        if (focus, intensity) != (self.trace_focus, self.trace_intensity):
            self.trace_focus = focus
            self.trace_intensity = intensity
            self.draw()

    def peak(self):
        if self.spectrum is not None:
            samples = self.spectrum.samples
            self.select_marker(max(range(len(samples)), key=samples.__getitem__))

    def select_marker(self, index):
        self.marker = index
        self.draw()
        self.marker_callback()

    def move_marker(self, offset):
        if self.spectrum is not None:
            index = self.marker if self.marker is not None else len(self.spectrum.samples) // 2
            self.select_marker(max(0, min(len(self.spectrum.samples) - 1, index + offset)))

    def place_marker(self, event):
        left, top, right, bottom = self.bounds
        if self.spectrum is not None and left <= event.x <= right and top <= event.y <= bottom:
            self.focus_set()
            fraction = (event.x - left) / (right - left)
            self.select_marker(round(fraction * (len(self.spectrum.samples) - 1)))

    def draw(self):
        self.delete("all")
        left, top = 65, 25
        right, bottom = self.winfo_width() - 24, self.winfo_height() - 58
        if right <= left or bottom <= top:
            return
        self.bounds = (left, top, right, bottom)
        width, height = right - left, bottom - top
        colors = self.colors
        spectrum = self.spectrum
        points = []
        if spectrum is not None:
            top_fraction = COUNTS_PER_DIVISION * VERTICAL_DIVISIONS
            for index, sample in enumerate(spectrum.samples):
                x = left + width * index / (len(spectrum.samples) - 1)
                fraction = min(1, max(0, (TOP_COUNT - sample) / top_fraction))
                points.extend((x, top + height * fraction))
            trace_fill = blend_trace_color(colors["trace_fill"], colors["screen"], self.trace_intensity)
            self.create_polygon(left, bottom, *points, right, bottom, fill=trace_fill)

        for division in range(HORIZONTAL_DIVISIONS + 1):
            x = left + width * division / HORIZONTAL_DIVISIONS
            self.create_line(x, top, x, bottom, fill=colors["cross" if division == 5 else "grid"])
        for division in range(VERTICAL_DIVISIONS + 1):
            y = top + height * division / VERTICAL_DIVISIONS
            self.create_line(left, y, right, y, fill=colors["cross" if division == 4 else "grid"])
            if spectrum and spectrum.scale_db:
                amplitude = -division * spectrum.scale_db
                if spectrum.reference_dbm is not None:
                    amplitude += spectrum.reference_dbm
                label = f"{amplitude:g}"
            else:
                label = f"{-division:g}"
            self.create_text(left - 9, y, text=label, fill=colors["readout"],
                             anchor="e", font=self.mono)

        for tick in range(1, 50):
            x = left + width * tick / 50
            size = 4 if tick % 5 == 0 else 2
            middle = (top + bottom) / 2
            self.create_line(x, middle - size, x, middle + size, fill=colors["cross"])
        for tick in range(1, 40):
            y = top + height * tick / 40
            size = 4 if tick % 5 == 0 else 2
            middle = (left + right) / 2
            self.create_line(middle - size, y, middle + size, y, fill=colors["cross"])

        show_frequency = spectrum is not None and spectrum.frequency(0) is not None
        for division in range(0, 11, 2):
            x = left + width * division / 10
            if show_frequency:
                frequency = spectrum.frequency(round((len(spectrum.samples) - 1) * division / 10))
                text = f"{frequency:.3f}".rstrip("0").rstrip(".")
            else:
                text = f"{division * 10}"
            self.create_text(x, bottom + 12, anchor="n", text=text,
                             fill=colors["readout"], font=self.mono)
        x_label = "frequency" if show_frequency else "sweep_position"
        if spectrum is not None and spectrum.span_mhz == 0:
            x_label = "time_position"
        self.create_text((left + right) / 2, bottom + 36, text=self.translate(x_label),
                         font=("Segoe UI", 9), fill=colors["readout"])
        y_label = "dBm" if spectrum and spectrum.reference_dbm is not None else self.translate("graticule_db")
        if not spectrum or not spectrum.scale_db:
            y_label = "DIV"
        self.create_text(left + 4, top - 11, text=y_label, anchor="w",
                         font=self.mono, fill=colors["readout"])

        if points:
            readout = self.translate("unknown_trace_mode" if spectrum.mode is None else f"mode_{spectrum.mode}")
            if spectrum.uncalibrated:
                readout += f"  ·  {self.translate('uncalibrated')}"
            self.create_text(right, top - 11, text=readout, anchor="e",
                             font=self.mono, fill=colors["warning" if spectrum.uncalibrated else "readout"])
            trace_color = blend_trace_color(colors["trace"], colors["screen"], self.trace_intensity)
            self.create_line(*points, fill=trace_color, width=TRACE_WIDTHS[self.trace_focus])
            if self.marker is not None:
                x, y = points[2 * self.marker:2 * self.marker + 2]
                self.create_line(x, top, x, bottom, fill=colors["marker"], dash=(4, 5))
                self.create_polygon(x, y - 7, x + 7, y, x, y + 7, x - 7, y,
                                    fill=colors["screen"], outline=colors["marker"], width=2)
        else:
            x, y = (left + right) / 2, (top + bottom) / 2
            self.create_text(x, y - 13, text=self.translate("trace_empty"),
                             font=("Segoe UI Semibold", 17), fill=colors["trace"])
            self.create_text(x, y + 20, text=self.translate(self.empty_hint),
                             font=("Segoe UI", 10), fill=colors["readout"],
                             width=max(200, width - 30))
