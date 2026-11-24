from dataclasses import dataclass


TOP_COUNT = 229
COUNTS_PER_DIVISION = 25
VERTICAL_DIVISIONS = 8
HORIZONTAL_DIVISIONS = 10


@dataclass(frozen=True)
class Spectrum:
    samples: tuple[int, ...]
    center_mhz: float | None
    span_mhz: float | None
    reference_dbm: float | None
    scale_db: float | None
    mode: int | None = None
    uncalibrated: bool = False

    def frequency(self, index: int) -> float | None:
        if self.center_mhz is None or not self.span_mhz:
            return None
        return self.center_mhz + self.span_mhz * (index / (len(self.samples) - 1) - 0.5)

    def level(self, index: int) -> float | None:
        if self.reference_dbm is None or self.scale_db is None:
            return None
        return self.reference_dbm + self.scale_db * (self.samples[index] - TOP_COUNT) / COUNTS_PER_DIVISION


def spectrum_from_trace(trace, state: dict) -> Spectrum:
    mode = state.get("vm")
    # Memory B can have its own setup. A-B is a difference display. The wire
    # block does not include the reference and span metadata for those modes.
    absolute = mode in (0, 3, 4)
    return Spectrum(
        tuple(trace.samples),
        trace.center_mhz if trace.center_mhz is not None else state.get("cf"),
        state.get("sp") if absolute else None,
        state.get("rl") if absolute else None,
        state.get("db"),
        mode,
        state.get("uc") == 1,
    )
