"""The cursor of a checkbox and of a slider's drag (port of ``checkbox.go``)."""

import math
import random
from typing import Dict, List, Optional, Sequence

from .jsontree import go_round
from .types import ICaptchaSensorData, IMouseTraceParams
from .utils import get_random_int

# The cursor trace is capped at 900 KB at about 20 bytes a point.
MAX_SENSOR_POINTS = 900 * 1024 // 20
# The trace step when VK names none.
DEFAULT_TRACE_INTERVAL_MS = 500


def _point(x: float, y: float) -> ICaptchaSensorData:
    return {"x": int(x), "y": int(y)}


def mouse_trace(interval_ms: int, duration_ms: int, from_pos=None, to_pos=None) -> List[ICaptchaSensorData]:
    """A jittery ease-out move between two random points, one point every
    interval_ms over duration_ms; an interval of 0 or less means 500 ms."""
    if interval_ms <= 0:
        interval_ms = DEFAULT_TRACE_INTERVAL_MS
    if from_pos is None:
        from_pos = {"x": get_random_int(1080 // 2, 1080), "y": get_random_int(720 // 2, 720)}
    if to_pos is None:
        to_pos = {
            "x": get_random_int(from_pos["x"] - 300, from_pos["x"] + 300),
            "y": get_random_int(from_pos["y"] - 300, from_pos["y"] + 300),
        }
    steps = int(duration_ms) // int(interval_ms)
    dx, dy = float(to_pos["x"] - from_pos["x"]), float(to_pos["y"] - from_pos["y"])
    points: List[ICaptchaSensorData] = []
    for step in range(steps):
        t = min(1.0, step / steps)
        eased = t * (2 - t)  # ease-out quad
        noise_x = (random.random() - 0.5) * 6
        noise_y = (random.random() - 0.5) * 6
        points.append(
            _point(
                go_round(from_pos["x"] + dx * eased + noise_x),
                go_round(from_pos["y"] + dy * eased + noise_y),
            )
        )
    return points


def checkbox_sensors(sensors: Sequence[str], interval_ms: int) -> Dict[str, List[ICaptchaSensorData]]:
    """The answer of a checkbox captcha: a mouse trace for "cursor" and an
    empty list for every other sensor VK asks for. interval_ms is VK's
    sensors_delay; 0 or less means 500 ms."""
    cursor = mouse_trace(interval_ms, get_random_int(2_000, 10_000))[:MAX_SENSOR_POINTS]
    return {s: (cursor if s == "cursor" else []) for s in sensors}


def _ease_out(t: float) -> float:
    return t * (2 - t)


def _ease_in_out(t: float) -> float:
    if t < 0.5:
        return 2 * t * t
    return -1 + (4 - 2 * t) * t


def drag_trace(delay_ms: float, span_s: float, frac: float) -> List[ICaptchaSensorData]:
    """The cursor the widget samples every delay_ms (its sensors_delay) over
    span_s seconds while a person moves to the slider's handle and drags it
    frac of the way: page coordinates in the profile's 1200x626 window, where
    VK's popup stands in the middle. The geometry is a guess: VK's popup was
    not measured, and VK has not been seen to check it."""
    if delay_ms <= 0:
        delay_ms = 200
    span_ms = int(span_s * 1000)  # Go's Duration.Milliseconds truncates
    n = min(max(int(span_ms / delay_ms), 1), MAX_SENSOR_POINTS)
    start = (random.randint(470, 520), random.randint(300, 340))  # about the checkbox
    handle = (random.randint(430, 470), random.randint(460, 500))  # the slider's handle
    to = (handle[0] + int(go_round(frac * random.randint(240, 280))), handle[1] + random.randint(-3, 3))
    move = max(n * 2 // 5, 1)
    points: List[ICaptchaSensorData] = []
    for i in range(n):
        if i < move:
            a, b, t = start, handle, _ease_out((i + 1) / move)
        else:
            a, b, t = handle, to, _ease_in_out((i - move + 1) / (n - move))
        points.append(
            _point(
                a[0] + int(go_round((b[0] - a[0]) * t)) + random.randint(-1, 1),
                a[1] + int(go_round((b[1] - a[1]) * t)) + random.randint(-1, 1),
            )
        )
    return points


def slider_fraction(swaps: Sequence[int], steps: Optional[Sequence[int]]) -> float:
    """How far along its track the slider stands for an answer of swaps, out
    of the content's swaps (its steps but the first)."""
    total = len(steps or []) - 1
    if total > 0:
        return min(len(swaps) / total, 1.0)
    return 0.0


class CheckboxCaptchaSolver:
    """The checkbox answer (kept for compatibility; checkbox_sensors does the work)."""

    def __init__(self) -> None:
        self.max_sensors_data_size_kb = 900

    def solve(
        self,
        sensors_list: List[str],
        mouse_trace_params: Optional[IMouseTraceParams] = None,
    ) -> Dict[str, List[ICaptchaSensorData]]:
        params = mouse_trace_params or {}
        cursor = self.generate_mouse_trace(params)
        max_points = math.floor(self.max_sensors_data_size_kb * 1024 / 20)
        cursor = cursor[:max_points]
        return {s: (cursor if s == "cursor" else []) for s in sensors_list}

    def generate_mouse_trace(self, params: Optional[IMouseTraceParams] = None) -> List[ICaptchaSensorData]:
        params = params or {}
        return mouse_trace(
            int(params.get("intervalMs") or DEFAULT_TRACE_INTERVAL_MS),
            int(params.get("durationMs") or get_random_int(2_000, 10_000)),
            params.get("from_"),
            params.get("to"),
        )
