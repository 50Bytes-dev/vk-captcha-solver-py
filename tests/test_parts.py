"""The slider, the cursor, the fake browser and the hash (ports of slider_test,
checkbox_test, fakeenv_test and x64hash128_test)."""

import base64
import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from conftest import encode_jpeg, encode_png, gradient, scramble
from vk_captcha_solver import VKCaptchaSolverError
from vk_captcha_solver.api import InitialData, form_json
from vk_captcha_solver.checkbox_solver import (
    MAX_SENSOR_POINTS,
    CheckboxCaptchaSolver,
    checkbox_sensors,
    drag_trace,
    mouse_trace,
    slider_fraction,
)
from vk_captcha_solver.jsontree import parse_json
from vk_captcha_solver.profile import resolve_timezone, timezone_name
from vk_captcha_solver.slider_solver import SliderCaptchaSolver, solve_slider
from vk_captcha_solver.solver import slider_answer
from vk_captcha_solver.utils import generate_fake_desktop_environment, x64hash128
from vk_captcha_solver.utils.fake_env import _components_to_canonical_string


def test_slider_scores_match_python():
    """On a picture whose tiles differ in size (343 is not a multiple of 5),
    every step's score must equal the vectors the Go port was checked
    against."""
    swaps = [0, 24, 3, 7, 12, 13, 4, 20, 1, 1, 99, 2, 18, 6, 9, 21]
    want = [140286, 182928, 210879, 302637, 302637, 302637, 422953, 460734]
    s = SliderCaptchaSolver()
    img = gradient(343, 217)
    layout = s.compute_tile_layout(343, 217, 5)
    perm = list(range(25))
    got = []
    for step in range(len(swaps) // 2):
        a, b = swaps[2 * step], swaps[2 * step + 1]
        if 0 <= a < 25 and 0 <= b < 25:
            perm[a], perm[b] = perm[b], perm[a]
        got.append(s.calculate_seam_score(s.apply_tile_permutation(img, layout, perm, 343, 217, "png"), 5))
    assert got == want
    assert solve_slider({"image": encode_png(img), "steps": [5] + swaps}) == [0, 24]


@pytest.mark.parametrize("encode", [encode_png, encode_jpeg], ids=["png", "jpeg"])
def test_slider_finds_the_unscrambled_step(encode):
    swaps = [0, 24, 3, 7, 12, 13, 1, 1, 4, 20, 2, 18]
    pic = scramble(gradient(300, 200), swaps, 3)
    assert solve_slider({"image": encode(pic), "steps": [5] + swaps}) == swaps[:6]


def test_slider_without_content_answers_no_swaps():
    assert solve_slider({"image": "", "steps": [5, 0, 1]}) == []
    assert solve_slider({"image": "aGVsbG8=", "steps": None}) == []
    assert slider_answer(None) == "eyJ2YWx1ZSI6W119"
    assert slider_answer([0, 24]) == "eyJ2YWx1ZSI6WzAsMjRdfQ=="


def test_slider_rejects_bad_picture():
    tiny = encode_png(gradient(4, 4))
    big = Image.new("1", (4097, 4097))
    import io

    buf = io.BytesIO()
    big.save(buf, "PNG")
    for name, img in {
        "not base64": "%%%",
        "not image": base64.b64encode(b"hello").decode(),
        "too small": tiny,
        "too large": base64.b64encode(buf.getvalue()).decode(),
    }.items():
        with pytest.raises(VKCaptchaSolverError):
            solve_slider({"image": img, "steps": [5, 0, 1]})
    # Base64 without padding, and with line breaks, is read.
    raw = encode_png(gradient(300, 200)).rstrip("=")
    assert solve_slider({"image": raw[:50] + "\n" + raw[50:], "steps": [5, 0, 1]}) == [0, 1]


def test_mouse_trace_shape():
    trace = mouse_trace(100, 2000)
    assert len(trace) == 20
    first = trace[0]
    assert 540 - 3 <= first["x"] <= 1080 + 3 and 360 - 3 <= first["y"] <= 720 + 3
    assert all(abs(p["x"] - first["x"]) <= 306 and abs(p["y"] - first["y"]) <= 306 for p in trace)
    assert len(mouse_trace(0, 2000)) == 4
    assert mouse_trace(500, 100) == []


def test_checkbox_sensors():
    sensors = checkbox_sensors(["cursor", "accelerometer", "keyboard"], 0)
    assert set(sensors) == {"cursor", "accelerometer", "keyboard"}
    assert 4 <= len(sensors["cursor"]) <= 20
    assert sensors["accelerometer"] == [] and sensors["keyboard"] == []
    # The compatibility class with a zero sensors_delay does not divide by zero.
    assert CheckboxCaptchaSolver().solve(["cursor"], {"intervalMs": 0})["cursor"]


def test_drag_trace():
    trace = drag_trace(200, 1.5, 0.5)
    assert len(trace) == 7 and all(set(p) == {"x", "y"} for p in trace)
    assert len(drag_trace(0, 0.0, 1)) == 1
    assert len(drag_trace(1, 10_000, 1)) == MAX_SENSOR_POINTS
    assert slider_fraction([0, 1, 2, 3], [5, 0, 1, 2, 3, 4, 5, 6, 7]) == 0.5
    assert slider_fraction([0, 1], [5]) == 0


FIXED = {
    "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "language": "ru-RU",
    "languages": ["ru-RU", "ru", "en", "en-US"],
    "colorDepth": 24,
    "deviceMemory": 8,
    "hardwareConcurrency": 12,
    "screenResolution": [1920, 1080],
    "availableScreenResolution": [1920, 1040],
    "timezone": "Europe/Moscow",
    "timezoneOffset": -180,
    "sessionStorage": True,
    "localStorage": False,
    "indexedDB": True,
    "openDatabase": False,
    "cpuClass": None,
    "platform": "MacIntel",
    "plugins": ["Chrome PDF Plugin", "Widevine Content Decryption Module"],
    "mimeTypes": ["application/pdf", "video/mp4", "audio/mpeg"],
    "canvas": "abcdefghijklmnopqrstuvwxyz012345",
    "webgl": "0123456789abcdefghijklmnopqrstuvwxyz0123",
    "webglVendorAndRenderer": {"vendor": "Apple Inc.", "renderer": "Apple GPU"},
    "audio": "zyxwvutsrqponmlkjihgfedc",
    "fonts": ["Arial", "Helvetica", "Comic Sans MS"],
    "touchSupport": {"maxTouchPoints": 0, "touchscreen": False},
    "webdriver": False,
    "pixelRatio": 1.25,
    "screenOrientation": {"angle": 0, "type": "landscape-primary"},
    "connectionEffectiveType": "4g",
    "notificationsPermission": "denied",
    "innerWidth": 1500,
    "innerHeight": 800,
}


def test_canonical_string_and_fingerprint():
    got = _components_to_canonical_string({k: {"value": v, "duration": 0} for k, v in FIXED.items()})
    assert got.startswith('audio:"zyxwvutsrqponmlkjihgfedc"|availableScreenResolution:[1920,1040]|')
    assert x64hash128(got) == "93f601b66af69479d4292914cd32fd18"
    assert _components_to_canonical_string({"a:b|c\\d": {"value": 1}}) == "a\\:b\\|c\\\\d:1"


def test_fake_environment_is_consistent():
    for _ in range(200):
        env = generate_fake_desktop_environment()
        assert env["screenAvailWidth"] == env["screenWidth"] and env["screenAvailHeight"] <= env["screenHeight"]
        assert env["innerWidth"] <= env["screenAvailWidth"] and env["innerHeight"] <= env["screenAvailHeight"]
    js = form_json(generate_fake_desktop_environment())
    order = [
        "screenWidth", "screenHeight", "screenAvailWidth", "screenAvailHeight", "innerWidth", "innerHeight",
        "devicePixelRatio", "language", "languages", "webdriver", "hardwareConcurrency", "deviceMemory",
        "connectionEffectiveType", "notificationsPermission",
    ]
    assert parse_json(js).keys == order


@pytest.mark.parametrize(
    "s,want",
    [
        ("", "00000000000000000000000000000000"),
        ("a", "85555565f6597889e6b53a48510e895a"),
        ("hello", "cbd8a7b341bd9b025b1e906a48ae1d19"),
        ("0123456789abcdef", "4be06d94cf4ad1a787c35b5c63a708da"),
        ("0123456789abcdefg", "8e32612daa45f9de0800f4c206c372ee"),
        ("Привет, мир", "8441badc62107cc1b135ae404c240b6c"),
        ("x" * 1000, "84da7741b76d628b7851694dc53b2444"),
    ],
)
def test_x64hash128(s, want):
    assert x64hash128(s) == want


def test_timezone(monkeypatch):
    monkeypatch.delenv("VK_CAPTCHA_TIMEZONE", raising=False)
    monkeypatch.delenv("TZ", raising=False)
    assert timezone_name() == "Europe/Moscow"
    assert resolve_timezone() == ("Europe/Moscow", -180)
    monkeypatch.setenv("TZ", "UTC")  # not a zone name the browser would give
    assert timezone_name() == "Europe/Moscow"
    monkeypatch.setenv("TZ", ":Asia/Yekaterinburg")
    assert resolve_timezone() == ("Asia/Yekaterinburg", -300)
    monkeypatch.setenv("VK_CAPTCHA_TIMEZONE", "Asia/Tbilisi")
    assert resolve_timezone() == ("Asia/Tbilisi", -240)
    assert resolve_timezone("Europe/Samara") == ("Europe/Samara", -240)
    winter, summer = datetime(2026, 1, 15, tzinfo=timezone.utc), datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert resolve_timezone("America/New_York", winter) == ("America/New_York", 300)
    assert resolve_timezone("America/New_York", summer) == ("America/New_York", 240)
    assert resolve_timezone("Not/AZone") == ("Europe/Moscow", -180)


def test_settings_of():
    tree = parse_json('[{"a":1.50,"b":[1,2]},null,"STR"]')
    d = InitialData(captcha_settings=[("slider", tree.items[0]), ("sound", tree.items[1]), ("str", tree.items[2])])
    assert form_json(d.settings_of("slider")) == '{"a":1.50,"b":[1,2]}'
    assert d.settings_of("sound") is None and d.settings_of("str") == "STR" and d.settings_of("x") is None
    d.content_settings = [("slider", "SLIDER-KEY")]
    assert d.settings_of("slider") == "SLIDER-KEY"
    assert json.loads(form_json({"v": [1.0, 2.5, "é<&>"]})) == {"v": [1, 2.5, "é<&>"]}
    assert form_json([7.0]) == "[7]"
