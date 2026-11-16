"""Solving a BFF captcha page (port of v2_test.go, second half)."""

import base64
import json
import time

import pytest

from conftest import (
    SLIDER_SWAPS,
    TEST_CAPTCHA,
    TEST_DEBUG_INFO,
    TEST_POW_INPUT,
    TEST_SESSION_TOKEN,
    Reply,
    attach,
    bff_captcha_page,
    new_bff_fake,
    server_context,
    slider_content_json,
)
from vk_captcha_solver import (
    FAILURE_BOT,
    FAILURE_OTHER,
    FAILURE_PAGE_CHANGED,
    FAILURE_TIMEOUT,
    CaptchaSolver,
    failure_kind,
)
from vk_captcha_solver.api import ACCEPT_LANGUAGE
from vk_captcha_solver.jsontree import jget, jnum, parse_json
from vk_captcha_solver.profile import DEFAULT_PROFILE as P
from vk_captcha_solver.profile import telemetry_hash
from vk_captcha_solver.solver import EMPTY_ANSWER, MIN_CLICK, MIN_DRAG, pow_search

PAGE = "id.vk.com/not_robot_captcha"


def decode_pow_v2(param: str, pow_input: str = TEST_POW_INPUT, difficulty: int = 2) -> dict:
    """The JSON inside a v2 hash parameter, its proof of work and tel_hash checked."""
    assert param.startswith("v2."), param[:40]
    env = json.loads(base64.b64decode(param[3:]))
    want, nonce = pow_search(pow_input, difficulty, 0)
    assert (env["hash"], env["nonce"], env["error"]) == (want, nonce, "")
    assert env["duration_ms"] > 0
    raw = base64.b64decode(param[3:]).decode("ascii")
    tel_text = raw[raw.index(',"telemetry":') + len(',"telemetry":') : raw.rindex(',"tel_hash":')]
    assert telemetry_hash(tel_text) == env["tel_hash"]
    env["telemetry_text"] = tel_text
    return env


async def started(f):
    await f.start(server_context())
    return f


async def test_solve_bff_checkbox(bff, make_solver):
    """A BFF page is solved as Chrome solved it: initSession, settings,
    componentDone, check and endSession on api.vk.com with v=5.131 in the
    query, the form in the widget's order with the solver's adFp and an empty
    access_token, the profile's browser and the v2 proof of work; the check
    waits for the time a person could click once the telemetry is done."""
    waits = []
    s = make_solver(bff, waits=waits)
    token, report = await s.solve_report(TEST_CAPTCHA)
    assert token == "SUCCESS-TOKEN"
    methods = [
        "captchaNotRobot.initSession",
        "captchaNotRobot.settings",
        "captchaNotRobot.componentDone",
        "captchaNotRobot.check",
        "captchaNotRobot.endSession",
    ]
    assert bff.call_keys() == [PAGE] + methods

    page = bff.call(PAGE)
    assert page.headers["Sec-Fetch-Mode"] == "navigate"
    assert page.headers["Sec-Fetch-Dest"] == "document"
    assert page.headers["Accept-Language"] == ACCEPT_LANGUAGE
    assert page.headers["User-Agent"] == P.user_agent
    assert page.headers["sec-ch-ua-platform"] == '"macOS"'

    init = bff.call("captchaNotRobot.initSession")
    assert init.host == "api.vk.com" and init.q("v") == "5.131" and init.f("v") is None
    assert init.f("session_token") == TEST_SESSION_TOKEN and init.f("domain") == "vk.com"
    assert init.f("lang") == "0" and init.f("access_token") == "" and init.f("adFp") is None

    ad_fp = None
    for m in methods[1:]:
        c = bff.call(m)
        assert c.host == "api.vk.com" and c.path == "/method/" + m and c.q("v") == "5.131"
        # VK's antibot answers BOT to a call without a fetch's headers.
        assert c.headers["Origin"] == "https://id.vk.com", m
        assert c.headers["Referer"] == "https://id.vk.com/", m
        assert c.headers["Accept"] == "*/*", m
        assert c.headers["User-Agent"] == P.user_agent, m
        assert c.headers["Sec-Ch-Ua-Platform"] == '"macOS"', m
        assert c.headers["Sec-Fetch-Mode"] == "cors" and c.headers["Sec-Fetch-Site"] == "same-site", m
        assert c.headers["Sec-Fetch-Dest"] == "empty" and c.headers["Priority"] == "u=1, i", m
        assert c.headers["Accept-Language"] == ACCEPT_LANGUAGE, m
        assert c.headers["Content-Type"] == "application/x-www-form-urlencoded", m
        assert len(c.f("adFp")) == 21 and c.f("adFp") == s._ad_fp, m
        assert ad_fp in (None, c.f("adFp"))
        ad_fp = c.f("adFp")
        assert c.f("access_token") == "", m

    done = bff.call("captchaNotRobot.componentDone")
    assert done.f("device") == P.device and done.f("browser_fp") == ""

    check = bff.call("captchaNotRobot.check")
    for sensor in ("accelerometer", "gyroscope", "motion", "cursor", "taps"):
        assert check.f(sensor) == "[]", sensor
    assert check.f("browser_fp") == P.browser_fp and check.f("answer") == EMPTY_ANSWER
    assert check.f("debug_info") == TEST_DEBUG_INFO and check.f("connectionRtt") is None
    env = decode_pow_v2(check.f("hash"))
    assert TEST_SESSION_TOKEN in env["telemetry_text"]  # stack_at holds the page address

    # The form goes in the order Chrome sent it (2026-11-03).
    for method, keys in {
        "captchaNotRobot.initSession": ["session_token", "domain", "lang", "access_token"],
        "captchaNotRobot.settings": ["session_token", "domain", "adFp", "access_token"],
        "captchaNotRobot.componentDone": ["session_token", "domain", "adFp", "browser_fp", "device", "access_token"],
        "captchaNotRobot.check": [
            "session_token", "domain", "adFp", "accelerometer", "gyroscope", "motion", "cursor", "taps",
            "browser_fp", "hash", "answer", "debug_info", "access_token",
        ],
        "captchaNotRobot.endSession": ["session_token", "domain", "adFp", "access_token"],
    }.items():
        assert bff.call(method).form_keys() == keys, method

    # The probes run in the page's order, and the check waits until a person
    # could click after the telemetry is done.
    tel = parse_json(env["telemetry_text"])
    assert tel.keys[:3] == ["ua", "frame", "globals"]  # the page's order, not the script's
    assert len(waits) == 1
    assert report.kind == "checkbox" and report.bff and report.script == "recorded-43"
    assert report.check_at >= (env["duration_ms"] + 300) / 1000 + MIN_CLICK - 0.001
    perf_origin = jnum(jget(tel, "client_clock_snapshot", "result", "perf_origin"))[0]
    assert abs(perf_origin / 1000 - time.time()) < 30

    # The next solving, and another solver of the process, are the same
    # browser: the same adFp.
    bff.calls.clear()
    await s.solve(TEST_CAPTCHA)
    assert bff.call("captchaNotRobot.check").f("adFp") == ad_fp
    other = make_solver(bff)
    bff.calls.clear()
    await other.solve(TEST_CAPTCHA)
    assert bff.call("captchaNotRobot.check").f("adFp") == ad_fp


async def test_solve_bff_bot_offers_slider(bff, make_solver):
    """A checkbox VK answers BOT and offers a slider for goes on to the slider
    as the widget does: getContent with the slider's settings_key,
    componentDone with the fingerprint, and a check with the slider's answer,
    the drag's cursor and the same proof of work, a moment for the drag later."""
    bff.set("captchaNotRobot.getContent", slider_content_json())
    bff.queue(
        "captchaNotRobot.check",
        Reply(body='{"response":{"status":"BOT","show_captcha_type":"slider","success_token":""}}'),
    )
    ahead = []
    s = make_solver(bff)

    async def wait(until):
        ahead.append(until - time.monotonic())

    s._wait = wait
    token, report = await s.solve_report(TEST_CAPTCHA)
    assert token == "SUCCESS-TOKEN"
    assert report.kind == "checkbox" and report.slider_after_bot and report.check_at > 0
    assert bff.call_keys() == [
        PAGE,
        "captchaNotRobot.initSession",
        "captchaNotRobot.settings",
        "captchaNotRobot.componentDone",
        "captchaNotRobot.check",
        "captchaNotRobot.getContent",
        "captchaNotRobot.componentDone",
        "captchaNotRobot.check",
        "captchaNotRobot.endSession",
    ]
    content = bff.call("captchaNotRobot.getContent")
    assert content.f("captcha_settings") == "SLIDER-KEY"
    assert content.form_keys() == ["session_token", "domain", "adFp", "captcha_settings", "access_token"]
    dones = bff.calls_of("captchaNotRobot.componentDone")
    checks = bff.calls_of("captchaNotRobot.check")
    assert dones[0].f("browser_fp") == "" and dones[1].f("browser_fp") == P.browser_fp
    assert checks[1].f("answer") != EMPTY_ANSWER and checks[1].f("hash") == checks[0].f("hash")
    assert json.loads(base64.b64decode(checks[1].f("answer"))) == {"value": SLIDER_SWAPS[:4]}
    cursor = json.loads(checks[1].f("cursor"))
    assert len(cursor) >= 3 and set(cursor[0]) == {"x", "y"}
    assert len(ahead) == 2 and ahead[1] >= MIN_DRAG - 0.05


@pytest.mark.parametrize("case", ["slider first", "slider after BOT"])
async def test_solve_bff_slider_answer_not_taken(case, make_solver):
    """A slider answer VK does not take comes back as BOT with the slider
    offered again: a wrong answer (failed), not bot, and no second answer."""
    not_taken = Reply(body='{"response":{"status":"BOT","show_captcha_type":"slider"}}')
    f = await started(new_bff_fake("slider" if case == "slider first" else "checkbox"))
    try:
        f.set("captchaNotRobot.getContent", slider_content_json())
        f.queue("captchaNotRobot.check", not_taken, not_taken)
        with pytest.raises(Exception) as ei:
            await make_solver(f).solve(TEST_CAPTCHA)
        assert failure_kind(ei.value) == FAILURE_OTHER
        assert "did not take the slider's answer" in str(ei.value)
        assert f.call_keys()[-1] == "captchaNotRobot.check"
        assert ei.value.report.kind == ("slider" if case == "slider first" else "checkbox")
    finally:
        await f.close()


async def test_solve_bff_bot_without_slider(bff, make_solver):
    """BOT with no other captcha offered is VK's last word: no slider, no
    endSession, failure bot."""
    bff.set("captchaNotRobot.check", '{"response":{"status":"BOT","show_captcha_type":""}}')
    with pytest.raises(Exception) as ei:
        await make_solver(bff).solve(TEST_CAPTCHA)
    assert failure_kind(ei.value) == FAILURE_BOT
    assert str(ei.value) == "[Status: BOT] (captchaNotRobot.check) Bad method status"
    keys = bff.call_keys()
    assert keys[-1] == "captchaNotRobot.check" and "captchaNotRobot.getContent" not in keys
    assert ei.value.report.failure == FAILURE_BOT


async def test_solve_bff_no_time_for_check(bff, make_solver):
    """A solving that cannot send its check when a person could, after the
    telemetry is done, sends none: failure timeout."""
    with pytest.raises(Exception) as ei:
        await make_solver(bff).solve(TEST_CAPTCHA, timeout=1.5)
    assert failure_kind(ei.value) == FAILURE_TIMEOUT
    assert "too little time left" in str(ei.value)
    assert "captchaNotRobot.check" not in bff.call_keys()


async def test_the_wait_is_real_by_default(bff):
    """Without a test's wait, the check goes no sooner than the telemetry is
    done plus a click (the default wait sleeps)."""
    s = attach(CaptchaSolver(), bff)
    from vk_captcha_solver.solver import wait_until

    s._wait = wait_until
    t0 = time.monotonic()
    token, report = await s.solve_report(TEST_CAPTCHA)
    took = time.monotonic() - t0
    assert token == "SUCCESS-TOKEN"
    assert took >= report.check_at - 0.05 >= 0.3 + MIN_CLICK


async def test_bff_page_with_other_probes(make_solver):
    """A page whose probes are not the profile's is VK's change: page_changed,
    before any captchaNotRobot call."""
    defs, calls = [], []
    for i, name in enumerate(P.tel.keys):
        if i < len(P.fast_probes):
            defs.append("'" + name + "':function(){return 1;}")
        else:
            calls.append("await _0x174e19('" + name + "',async function(){return 1;});")

    def script(defs, calls):
        return bff_captcha_page().replace(
            "window['captchaPowResult']='v2.';", "var p={" + ",".join(defs) + "};" + "".join(calls), 1
        )

    pages = {
        "unknown probe in the order": bff_captcha_page().replace('["ua","frame","globals"]', '["globals","new_probe"]', 1),
        "a new probe": script(defs + ["'new_probe':function(){return 1;}"], calls),
        "a probe gone": script(defs[1:], calls),
    }
    for name, page in pages.items():
        f = await started(new_bff_fake("checkbox"))
        try:
            f.set(PAGE, page)
            with pytest.raises(Exception) as ei:
                await make_solver(f).solve(TEST_CAPTCHA)
            assert failure_kind(ei.value) == FAILURE_PAGE_CHANGED, name
            assert f.call_keys() == [PAGE], name
            assert TEST_SESSION_TOKEN not in str(ei.value) and ei.value.body == page
        finally:
            await f.close()

    f = await started(new_bff_fake("checkbox"))
    try:
        f.set(PAGE, script(defs, calls))
        assert await make_solver(f).solve(TEST_CAPTCHA) == "SUCCESS-TOKEN"
    finally:
        await f.close()


async def test_solve_bff_slider(make_solver):
    """The slider of a BFF page asks getContent with the slider's settings_key."""
    f = await started(new_bff_fake("slider"))
    try:
        f.set("captchaNotRobot.getContent", slider_content_json())
        waits = []
        s = make_solver(f, waits=waits)
        _, report = await s.solve_report(TEST_CAPTCHA)
        assert f.call("captchaNotRobot.getContent").f("captcha_settings") == "SLIDER-KEY"
        check = f.call("captchaNotRobot.check")
        assert check.f("answer") not in (EMPTY_ANSWER, "", None)
        assert json.loads(check.f("cursor"))
        assert report.kind == "slider" and not report.slider_after_bot and len(waits) == 1
        assert f.call_keys()[-1] == "captchaNotRobot.endSession"
    finally:
        await f.close()


@pytest.mark.parametrize("reply", [Reply(hang=True), Reply(status=503)], ids=["hang", "503"])
async def test_captcha_page_loaded_again_after_hang_or_5xx(reply, bff, make_solver):
    """A captcha page that hangs or answers 5xx is loaded once more, each try
    within the page timeout, and the captcha is solved."""
    bff.queue(PAGE, reply)
    s = make_solver(bff, {"pageTimeout": 0.3})
    t0 = time.monotonic()
    token = await s.solve(TEST_CAPTCHA, timeout=5)
    assert token == "SUCCESS-TOKEN"
    assert time.monotonic() - t0 < 2, "the hung try was not cut at the page timeout"
    keys = bff.call_keys()
    assert keys[:2] == [PAGE, PAGE] and PAGE not in keys[2:]


async def test_captcha_page_not_loaded_again_beyond_two_tries(make_solver):
    """The page is loaded at most twice, and a 4xx or a solving out of time is
    not tried again."""

    async def run(replies, options=None, timeout=None):
        f = await started(new_bff_fake("checkbox"))
        try:
            f.queue(PAGE, *replies)
            with pytest.raises(Exception) as ei:
                await make_solver(f, options).solve(TEST_CAPTCHA, timeout=timeout)
            return ei.value, f.call_keys().count(PAGE)
        finally:
            await f.close()

    err, pages = await run([Reply(status=502)] * 3)
    assert pages == 2 and failure_kind(err) == FAILURE_TIMEOUT and err.status == 502
    err, pages = await run([Reply(status=403)])
    assert pages == 1 and failure_kind(err) == FAILURE_OTHER
    err, pages = await run([Reply(hang=True)] * 2, {"pageTimeout": 2}, timeout=0.3)
    assert pages == 1 and failure_kind(err) == FAILURE_TIMEOUT


async def test_window_init_free_bff_page_without_session_token(make_solver):
    """A BFF page whose address has no session_token stops before the API."""
    f = await started(new_bff_fake("checkbox"))
    try:
        with pytest.raises(Exception) as ei:
            await make_solver(f).solve("https://id.vk.com/not_robot_captcha?domain=vk.com&variant=popup")
        assert failure_kind(ei.value) == FAILURE_PAGE_CHANGED
        assert "the address has no session_token" in str(ei.value)
        assert f.call_keys() == [PAGE]
    finally:
        await f.close()


async def test_unknown_captcha_type_of_bff_page(make_solver):
    f = await started(new_bff_fake("sound"))
    try:
        with pytest.raises(Exception) as ei:
            await make_solver(f).solve(TEST_CAPTCHA)
        assert failure_kind(ei.value) == FAILURE_PAGE_CHANGED
        assert str(ei.value) == 'captcha type "sound": unknown captcha type'
        assert f.call_keys() == [PAGE, "captchaNotRobot.initSession"]
    finally:
        await f.close()
