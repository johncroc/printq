"""Measure how high the bed sits in a camera frame, without a model.

Klipper's Z cannot be trusted once the motors have been released: the bed may have sunk, or been moved
by hand, in either direction. The camera can. As the bed rises, its far edge climbs the frame, so the
row of that edge is a direct reading of bed height. The two reference frames calibrate it:

    park reference    the bed out of view
    clear reference   the empty bed at inspection height

Their difference is the bed, which gives the bed's colour; the far edge in the clear reference is the
row the bed is raised towards and never past.

The reading depends on the bed's colour, so a different plate or different lighting needs new reference
frames. A frame in which the bed cannot be found measures as None, never as a height.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

SIZE = (432, 240)  # every frame is measured at this size; rows are reported as a fraction of the height


def _pixels(jpeg: bytes) -> np.ndarray:
    return np.asarray(Image.open(io.BytesIO(jpeg)).convert("RGB").resize(SIZE), dtype=float)


def _chroma(a: np.ndarray) -> np.ndarray:
    return a / (a.sum(axis=-1, keepdims=True) + 1e-6)


def bed_colour(park: bytes, clear: bytes) -> np.ndarray | None:
    """Chromaticity of the bed: the median colour of what the clear reference shows and the park one does not."""
    a, b = _pixels(park), _pixels(clear)
    mask = np.abs(a - b).sum(axis=2) > 120
    if mask.mean() < 0.05:
        return None  # the references do not differ: no bed to learn
    return _chroma(np.median(b[mask], axis=0))


def edge_row(jpeg: bytes, colour: np.ndarray) -> float | None:
    """Far edge of the bed as a fraction of frame height (0 = top). None when no bed is found."""
    a = _pixels(jpeg)
    bed = (np.abs(_chroma(a) - colour).sum(axis=2) < 0.07) & (a.sum(axis=2) > 90)
    rows = np.where(bed.sum(axis=1) >= 0.10 * SIZE[0])[0]
    return round(float(rows[0]) / SIZE[1], 4) if len(rows) else None


class Gauge:
    """Bed height by camera for one printer. `ok` is False when the references cannot calibrate it."""

    def __init__(self, references: dict):
        self.colour = self.target = None
        if "park" in references and "clear" in references:
            self.colour = bed_colour(references["park"], references["clear"])
            if self.colour is not None:
                self.target = edge_row(references["clear"], self.colour)
        self.ok = self.target is not None

    def row(self, jpeg: bytes) -> float | None:
        return edge_row(jpeg, self.colour) if self.ok else None
