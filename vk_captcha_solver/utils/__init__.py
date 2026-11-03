from .fake_env import (
    generate_fake_desktop_environment,
    generate_fake_fingerprint_from_environment,
)
from .number import get_random_float, get_random_int, get_random_number
from .safe_json import safe_json_parse
from .x64hash128 import x64hash128

__all__ = [
    "generate_fake_desktop_environment",
    "generate_fake_fingerprint_from_environment",
    "get_random_float",
    "get_random_int",
    "get_random_number",
    "safe_json_parse",
    "x64hash128",
]
