"""Solving the old captcha page (port of solver_test.go)."""

import base64
import json
import re

import pytest

from conftest import (
    CAPTCHA_SCRIPT,
    TEST_CAPTCHA,
    TEST_DEBUG_INFO,
    TEST_POW_HASH,
    TEST_POW_INPUT,
    TEST_SESSION_TOKEN,
    captcha_page,
    encode_png,
    gradient,
    scramble,
)
from vk_captcha_solver import (
    FAILURE_PAGE_CHANGED,
    FAILURE_TIMEOUT,
    APIError,
    HTTPError,
    UnknownCaptchaTypeError,
    VKCaptchaSolverError,
    failure_kind,
)
from vk_captcha_solver import api as api_module
from vk_captcha_solver.api import REMIXMDEVICE
from vk_captcha_solver.profile import DEFAULT_PROFILE as P
from vk_captcha_solver.solver import EMPTY_ANSWER, pow_search, proof_of_work

SCRIPT = "static.vk.com/vkid/1.1.1276/not_robot_captcha.js"
PAGE = "id.vk.com/not_robot_captcha"
CHECKBOX_CALLS = [
    PAGE,
    SCRIPT,
    "captchaNotRobot.settings",
    "captchaNotRobot.componentDone",
    "captchaNotRobot.check",
    "captchaNotRobot.endSession",
]


async def test_solve_checkbox(fake, make_solver):
    token = await make_solver(fake).solve(TEST_CAPTCHA)
    assert token == "SUCCESS-TOKEN"
    assert fake.call_keys() == CHECKBOX_CALLS
    for c in fake.calls:
        assert c.headers["User-Agent"] == P.user_agent, c
        assert c.cookies.get("remixmdevice") == REMIXMDEVICE, c
        assert "remixmdevice=" + REMIXMDEVICE in c.cookie_header, "the cookie goes raw, as a browser sends it"
        if c.host != "api.vk.ru":
            continue
        assert c.http_method == "POST" and c.f("v") == "5.199", c
        assert c.f("session_token") == TEST_SESSION_TOKEN and c.f("domain") == "vk.com", c
        assert c.f("adFp") is None and c.f("access_token") is None, c
        keys = c.form_keys()
        assert keys == sorted(keys), "the old page's form goes in key order (%s)" % c

    device = json.loads(fake.call("captchaNotRobot.componentDone").f("device"))
    assert device["webdriver"] is False and device["connectionEffectiveType"] == "4g" and len(device) == 14

    check = fake.call("captchaNotRobot.check")
    assert check.f("hash") == TEST_POW_HASH
    assert check.f("answer") == EMPTY_ANSWER and check.f("debug_info") == TEST_DEBUG_INFO
    assert re.fullmatch(r"[0-9a-f]{32}", check.f("browser_fp"))
    # sensors_delay 100 ms over 2-10 s.
    assert 20 <= len(json.loads(check.f("cursor"))) <= 100
    for s in ("accelerometer", "gyroscope", "motion", "taps", "keyboard"):
        assert check.f(s) == "[]", s
    downlink = json.loads(check.f("connectionDownlink"))
    assert 5 <= len(downlink) <= 10 and 3 <= downlink[0] <= 10
    assert not re.search(r"\.0[,\]]", check.f("connectionDownlink")), "numbers as JavaScript writes them"
    rtt = json.loads(check.f("connectionRtt"))
    assert rtt == [] or (5 <= len(rtt) <= 10 and 40 <= rtt[0] <= 60)


async def test_solve_slider(fake, make_solver):
    swaps = [0, 24, 3, 7, 12, 13, 4, 20, 1, 6]
    content = {"status": "OK", "extension": "png", "image": encode_png(scramble(gradient(300, 200), swaps, 2)), "steps": [5] + swaps}
    fake.set(PAGE, captcha_page("slider", "https://" + SCRIPT))
    fake.set("captchaNotRobot.getContent", json.dumps({"response": content}))
    await make_solver(fake).solve(TEST_CAPTCHA)
    assert fake.call_keys() == [
        PAGE,
        SCRIPT,
        "captchaNotRobot.settings",
        "captchaNotRobot.getContent",
        "captchaNotRobot.componentDone",
        "captchaNotRobot.check",
        "captchaNotRobot.endSession",
    ]
    assert fake.call("captchaNotRobot.getContent").f("captcha_settings") == "SLIDER-SETTINGS"
    # The first k = 2 swaps, as compact JSON.
    assert base64.b64decode(fake.call("captchaNotRobot.check").f("answer")) == b'{"value":[0,24,3,7]}'


async def test_solve_reads_script_once(fake, make_solver):
    """The script is read once per address: its version is in the address."""
    s = make_solver(fake)
    for _ in range(2):
        await s.solve(TEST_CAPTCHA)
    assert sum(k.endswith("not_robot_captcha.js") for k in fake.call_keys()) == 1
    # A new version is a new address, read anew.
    fake.set(PAGE, captcha_page("checkbox", "https://static.vk.com/vkid/1.1.1277/not_robot_captcha.js"))
    fake.set("static.vk.com/vkid/1.1.1277/not_robot_captcha.js", CAPTCHA_SCRIPT)
    await s.solve(TEST_CAPTCHA)
    assert "static.vk.com/vkid/1.1.1277/not_robot_captcha.js" in fake.call_keys()


def test_debug_info_cache_is_bounded():
    from vk_captcha_solver import CaptchaSolver

    s = CaptchaSolver()
    for i in range(16):
        s._cache_debug_info("u%d" % i, "v%d" % i)
    assert s._cached_debug_info("u0") == "v0" and len(s._debug_infos) == 16
    s._cache_debug_info("u16", "v16")  # a full cache starts over
    assert s._cached_debug_info("u0") is None and s._cached_debug_info("u16") == "v16"


async def test_solve_resolves_script_after_redirect(fake, make_solver):
    """The script address may be relative; it resolves against the page address
    after redirects."""
    fake.set("id.vk.com/captcha/page", captcha_page("checkbox", "/vkid/2/not_robot_captcha.js"))
    fake.set("id.vk.com/vkid/2/not_robot_captcha.js", CAPTCHA_SCRIPT)
    fake.status[PAGE] = 302
    fake.headers[PAGE] = [("Location", "https://id.vk.com/captcha/page")]
    await make_solver(fake).solve(TEST_CAPTCHA)
    keys = fake.call_keys()
    assert keys[1] == "id.vk.com/captcha/page" and keys[2] == "id.vk.com/vkid/2/not_robot_captcha.js"


async def test_unknown_type_stops_before_api(fake, make_solver):
    fake.set(PAGE, captcha_page("sound", "https://" + SCRIPT))
    with pytest.raises(UnknownCaptchaTypeError) as ei:
        await make_solver(fake).solve(TEST_CAPTCHA)
    assert failure_kind(ei.value) == FAILURE_PAGE_CHANGED
    assert fake.call_keys() == [PAGE, SCRIPT]


def _setup_status(code):
    def setup(f):
        f.status[PAGE] = code

    return setup


ERROR_CASES = {
    "API error object": (
        lambda f: f.set("captchaNotRobot.settings", '{"error":{"error_code":100,"error_msg":"One of the parameters specified was missing or invalid"}}'),
        lambda e: isinstance(e, APIError) and e.code == 100 and e.method == "captchaNotRobot.settings"
        and str(e) == "[Code: 100] (captchaNotRobot.settings) One of the parameters specified was missing or invalid",
        3,
    ),
    "check status not OK": (
        lambda f: f.set("captchaNotRobot.check", '{"response":{"status":"BOT"}}'),
        lambda e: isinstance(e, APIError) and e.status == "BOT" and e.method == "captchaNotRobot.check"
        and str(e) == "[Status: BOT] (captchaNotRobot.check) Bad method status",
        5,
    ),
    "check BOT offers a slider": (
        lambda f: f.set("captchaNotRobot.check", '{"response":{"status":"BOT","show_captcha_type":"slider"}}'),
        lambda e: str(e) == "[Status: BOT] (captchaNotRobot.check) Bad method status; show_captcha_type slider"
        and e.show_captcha_type == "slider",
        5,
    ),
    "no success_token": (
        lambda f: f.set("captchaNotRobot.check", '{"response":{"status":"OK"}}'),
        lambda e: type(e) is VKCaptchaSolverError and str(e) == "captchaNotRobot.check: no success_token",
        5,
    ),
    "page HTTP 500": (
        _setup_status(500),
        # A 5xx page gets a second try.
        lambda e: isinstance(e, HTTPError) and e.status == 500 and e.url == TEST_CAPTCHA
        and str(e) == "HTTP Error 500: Internal Server Error",
        2,
    ),
    "page without powInput": (
        lambda f: f.set(PAGE, "<html>nothing</html>"),
        # The page is an attribute, to see what VK changed; the text has no URL.
        lambda e: str(e) == "captcha page: powInput not found" and e.body == "<html>nothing</html>"
        and TEST_SESSION_TOKEN in e.url and failure_kind(e) == FAILURE_PAGE_CHANGED,
        1,
    ),
    "window.init without session_token": (
        lambda f: f.set(PAGE, captcha_page("checkbox", "https://" + SCRIPT).replace(TEST_SESSION_TOKEN, "")),
        lambda e: str(e) == "captcha page: window.init has no session_token" and failure_kind(e) == FAILURE_PAGE_CHANGED,
        1,
    ),
    "script without debug_info": (
        lambda f: f.set(SCRIPT, "var a = 1;"),
        lambda e: str(e) == "captcha script: debug_info not found" and failure_kind(e) == FAILURE_PAGE_CHANGED,
        2,
    ),
    "API answer not JSON": (
        lambda f: f.set("captchaNotRobot.settings", "<html>"),
        lambda e: str(e) == "captchaNotRobot.settings: response is not JSON",
        3,
    ),
}


@pytest.mark.parametrize("name", list(ERROR_CASES))
async def test_solve_errors(name, fake, make_solver):
    setup, check, calls = ERROR_CASES[name]
    setup(fake)
    with pytest.raises(VKCaptchaSolverError) as ei:
        await make_solver(fake).solve(TEST_CAPTCHA)
    assert check(ei.value), "%s: %r" % (name, ei.value)
    assert len(fake.call_keys()) == calls, fake.call_keys()
    assert TEST_SESSION_TOKEN not in str(ei.value)


async def test_response_size_is_bounded(fake, make_solver, monkeypatch):
    monkeypatch.setattr(api_module, "MAX_BODY_BYTES", 1000)
    with pytest.raises(VKCaptchaSolverError) as ei:
        await make_solver(fake).solve(TEST_CAPTCHA)
    assert str(ei.value) == "captcha page: response is too large"


async def test_network_error_text_has_no_url(make_solver):
    """A dead server: a transport failure whose text has no address."""
    from conftest import FakeVK

    dead = FakeVK()
    dead.port = 1  # nothing listens there
    s = make_solver(dead, {"pageTimeout": 1})
    with pytest.raises(VKCaptchaSolverError) as ei:
        await s.solve(TEST_CAPTCHA, timeout=5)
    assert ei.value.transport and failure_kind(ei.value) == FAILURE_TIMEOUT
    assert TEST_SESSION_TOKEN not in str(ei.value) and "://" not in str(ei.value)
    assert str(ei.value).startswith("captcha page: ")


async def test_options_headers_and_cookies(fake, make_solver):
    s = make_solver(
        fake,
        {
            "userAgent": "UA/1",
            "version": "5.250",
            "headers": {"Accept-Language": "ru", "User-Agent": "ignored"},
            "cookies": {"remixlang": "0"},
        },
    )
    await s.solve(TEST_CAPTCHA)
    for c in fake.calls:
        assert c.headers["User-Agent"] == "UA/1" and c.headers["Accept-Language"] == "ru", c
        assert "sec-ch-ua" not in c.headers, "client hints go with the profile's User-Agent only"
        assert c.cookies.get("remixlang") == "0" and c.cookies.get("remixmdevice") == REMIXMDEVICE, c
        if c.host == "api.vk.ru":
            assert c.f("v") == "5.250"


async def test_option_api_base(fake, make_solver):
    s = make_solver(fake, {"baseUrl": "https://api.vk.com/"})
    await s.solve(TEST_CAPTCHA)
    assert fake.call("captchaNotRobot.check").host == "api.vk.com"


def test_proof_of_work_matches_python():
    for inp, d, want in [
        (TEST_POW_INPUT, 2, TEST_POW_HASH),
        ("abc", 4, "00007e6516048fbcbdc5b9e74f8de7f539cba9503fba4c2b418ff2fc4e55d141"),
        # Nonces start at 1: difficulty 0 takes sha256("abc1").
        ("abc", 0, "dbfcfd0d87220f629339bd3adcf452d083fde3246625fb3a93e314f833e20d37"),
    ]:
        assert proof_of_work(inp, d) == want


def test_proof_of_work_stops():
    import threading
    import time

    with pytest.raises(VKCaptchaSolverError) as ei:
        pow_search("x", 65, 1)
    assert failure_kind(ei.value) == FAILURE_PAGE_CHANGED
    with pytest.raises(VKCaptchaSolverError) as ei:
        pow_search("x", 64, 0, deadline=time.monotonic() - 1)
    assert failure_kind(ei.value) == FAILURE_TIMEOUT
    stop = threading.Event()
    stop.set()
    with pytest.raises(VKCaptchaSolverError):
        pow_search("x", 64, 0, stop=stop)
