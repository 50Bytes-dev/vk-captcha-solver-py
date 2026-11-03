import json
import os
import random
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from ..types import IFakeBrowserEnvironment
from .number import get_random_int
from .x64hash128 import x64hash128

_LOCALES = ["en-US", "ru-RU"]
_RESOLUTIONS: List[Tuple[int, int]] = [
    (1920, 1080),
    (1366, 768),
    (1440, 900),
    (1536, 864),
    (1280, 720),
    (2560, 1440),
    (3840, 2160),
    (1600, 900),
    (1280, 1024),
    (2048, 1152),
]
_FONTS = [
    "Arial",
    "Times New Roman",
    "Courier New",
    "Helvetica",
    "Verdana",
    "Georgia",
    "Trebuchet MS",
    "Palatino",
    "Garamond",
    "Comic Sans MS",
]


def _random_bool() -> bool:
    return random.random() > 0.5


def _js_stringify(value: Any) -> str:
    """Аналог JSON.stringify (без пробелов, без ASCII-экранирования)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _components_to_canonical_string(components: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    for key in sorted(components):
        component = components[key]
        value = "error" if "error" in component else _js_stringify(component["value"])
        escaped_key = re.sub(r"([:|\\])", r"\\\1", key)
        parts.append(f"{escaped_key}:{value}")
    return "|".join(parts)


def _local_timezone_name() -> str:
    """IANA-имя локальной таймзоны (аналог Intl...resolvedOptions().timeZone)."""
    tz = os.environ.get("TZ")
    if tz and "/" in tz:
        return tz.lstrip(":")
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "Europe/Moscow"


def _timezone_offset_minutes() -> int:
    """Аналог Date.prototype.getTimezoneOffset() (минуты, UTC - local)."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return time.timezone // 60
    return -int(offset.total_seconds() // 60)


def _random_hash(length: int = 16) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(random.choice(alphabet) for _ in range(length))


def generate_fake_desktop_environment() -> IFakeBrowserEnvironment:
    screen_width, screen_height = random.choice(_RESOLUTIONS)

    screen_avail_width = screen_width
    screen_avail_height = int(screen_height * (0.93 + random.random() * 0.06))

    inner_width = int(screen_avail_width * (0.65 + random.random() * 0.35))
    inner_height = int(screen_avail_height * (0.65 + random.random() * 0.35))

    device_pixel_ratio = random.choice([1, 1.25, 1.5, 1.75, 2])

    language = random.choice(_LOCALES)
    base_lang = language.split("-")[0] or "en"
    languages = list(dict.fromkeys([language, base_lang, "en", "en-US"]))

    hardware_concurrency = random.choice([2, 4, 6, 8, 12, 16])
    device_memory = random.choice([4, 8, 16, 32])

    return {
        "screenWidth": screen_width,
        "screenHeight": screen_height,
        "screenAvailWidth": screen_avail_width,
        "screenAvailHeight": screen_avail_height,
        "innerWidth": inner_width,
        "innerHeight": inner_height,
        "devicePixelRatio": device_pixel_ratio,
        "language": language,
        "languages": languages,
        "webdriver": False,
        "hardwareConcurrency": hardware_concurrency,
        "deviceMemory": device_memory,
        "connectionEffectiveType": "4g",
        "notificationsPermission": "denied",
    }


def generate_fake_fingerprint_from_environment(env: IFakeBrowserEnvironment) -> str:
    platform = "MacIntel" if env["hardwareConcurrency"] >= 8 else "Win32"
    pixel_depth = [24, 32][get_random_int(0, 1)]
    user_agent = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        if platform == "MacIntel"
        else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    is_mac = platform == "MacIntel"

    components: Dict[str, Dict[str, Any]] = {
        "userAgent": {"value": user_agent, "duration": get_random_int(1, 5)},
        "language": {"value": env["language"], "duration": 0},
        "languages": {"value": env["languages"], "duration": 0},
        "colorDepth": {"value": pixel_depth, "duration": 0},
        "deviceMemory": {"value": env["deviceMemory"], "duration": 0},
        "hardwareConcurrency": {"value": env["hardwareConcurrency"], "duration": 0},
        "screenResolution": {
            "value": [env["screenWidth"], env["screenHeight"]],
            "duration": 0,
        },
        "availableScreenResolution": {
            "value": [env["screenAvailWidth"], env["screenAvailHeight"]],
            "duration": 0,
        },
        "timezone": {"value": _local_timezone_name(), "duration": get_random_int(1, 3)},
        "timezoneOffset": {"value": _timezone_offset_minutes(), "duration": 0},
        "sessionStorage": {"value": _random_bool(), "duration": 0},
        "localStorage": {"value": _random_bool(), "duration": 0},
        "indexedDB": {"value": _random_bool(), "duration": 0},
        "openDatabase": {"value": _random_bool(), "duration": 0},
        "cpuClass": {"value": None, "duration": 0},
        "platform": {"value": platform, "duration": 0},
        "plugins": {
            "value": ["Chrome PDF Plugin", "Widevine Content Decryption Module"],
            "duration": get_random_int(2, 6),
        },
        "mimeTypes": {
            "value": ["application/pdf", "video/mp4", "audio/mpeg"],
            "duration": get_random_int(1, 4),
        },
        "canvas": {"value": _random_hash(32), "duration": get_random_int(3, 10)},
        "webgl": {"value": _random_hash(40), "duration": get_random_int(5, 12)},
        "webglVendorAndRenderer": {
            "value": {
                "vendor": "Apple Inc." if is_mac else "Google Inc.",
                "renderer": "Apple GPU" if is_mac else "WebKit WebGL",
            },
            "duration": 0,
        },
        "audio": {"value": _random_hash(24), "duration": get_random_int(4, 9)},
        "fonts": {
            "value": [f for f in _FONTS if random.random() > 0.2],
            "duration": get_random_int(8, 20),
        },
        "touchSupport": {
            "value": {"maxTouchPoints": 0, "touchscreen": False},
            "duration": 0,
        },
        "webdriver": {"value": env["webdriver"], "duration": 0},
        "pixelRatio": {"value": env["devicePixelRatio"], "duration": 0},
        "screenOrientation": {
            "value": {"angle": 0, "type": "landscape-primary"},
            "duration": 0,
        },
        "connectionEffectiveType": {
            "value": env["connectionEffectiveType"],
            "duration": 0,
        },
        "notificationsPermission": {
            "value": env["notificationsPermission"],
            "duration": 0,
        },
        "innerWidth": {"value": env["innerWidth"], "duration": 0},
        "innerHeight": {"value": env["innerHeight"], "duration": 0},
    }

    return x64hash128(_components_to_canonical_string(components))
