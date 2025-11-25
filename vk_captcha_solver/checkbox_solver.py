import math
import random
from typing import Dict, List, Optional

from .types import IMouseTraceParams, ICaptchaSensorData
from .utils import get_random_number


class CheckboxCaptchaSolver:
    def __init__(self):
        self.max_sensors_data_size_kb = 900

    def solve(
        self,
        sensors_list: List[str],
        mouse_trace_params: Optional[IMouseTraceParams] = None,
    ) -> Dict[str, List[ICaptchaSensorData]]:
        cursor = self.generate_mouse_trace(mouse_trace_params)

        max_bytes = self.max_sensors_data_size_kb * 1024
        avg_bytes_per_point = 20

        max_points = math.floor(max_bytes / avg_bytes_per_point)

        if len(cursor) > max_points:
            cursor = cursor[:max_points]

        sensors: Dict[str, List[ICaptchaSensorData]] = {}

        for sensor in sensors_list:
            sensors[sensor] = cursor if sensor == "cursor" else []

        return sensors

    def generate_mouse_trace(
        self, params: Optional[IMouseTraceParams] = None
    ) -> List[ICaptchaSensorData]:
        params = params or {}

        from_pos = params.get("from_")
        to_pos = params.get("to")
        interval_ms = params.get("intervalMs", 500)
        duration_ms = params.get("durationMs", get_random_number(2000, 15000))

        if from_pos is None:
            from_pos = {
                "x": get_random_number(1080 // 2, 1080),
                "y": get_random_number(720 // 2, 720),
            }

        if to_pos is None:
            to_pos = {
                "x": get_random_number(from_pos["x"] - 300, from_pos["x"] + 300),
                "y": get_random_number(from_pos["y"] - 300, from_pos["y"] + 300),
            }

        total_steps = math.floor(duration_ms / interval_ms)
        dx = to_pos["x"] - from_pos["x"]
        dy = to_pos["y"] - from_pos["y"]

        points: List[ICaptchaSensorData] = []

        for step in range(total_steps):
            t = min(1, step / total_steps)

            # Easing: t * (2 - t)  (EaseOutQuad logic)
            eased_t = t * (2 - t)

            noise_x = (random.random() - 0.5) * 6
            noise_y = (random.random() - 0.5) * 6

            x = round(from_pos["x"] + dx * eased_t + noise_x)
            y = round(from_pos["y"] + dy * eased_t + noise_y)

            points.append({"x": x, "y": y})

        return points
