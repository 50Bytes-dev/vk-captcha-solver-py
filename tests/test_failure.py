"""Failure kinds and reports (port of failure_test.go)."""

import asyncio

import pytest

from conftest import TEST_CAPTCHA, Reply
from vk_captcha_solver import (
    FAILURE_BOT,
    FAILURE_OTHER,
    FAILURE_PAGE_CHANGED,
    FAILURE_TIMEOUT,
    APIError,
    HTTPError,
    Report,
    UnknownCaptchaTypeError,
    VKCaptchaSolverError,
    failure_kind,
)
from vk_captcha_solver.exceptions import net_error, page_error


def test_failure_kinds():
    wrapped = VKCaptchaSolverError(op="solve", cause=VKCaptchaSolverError(op="proof of work", cause=asyncio.TimeoutError()))
    for name, err, want in [
        ("BOT", APIError({"status": "BOT", "error_msg": "Bad method status"}, "captchaNotRobot.check"), FAILURE_BOT),
        ("bot, any case", APIError({"status": "bot", "error_msg": "x"}, "captchaNotRobot.check"), FAILURE_BOT),
        ("page without value", page_error("captcha page: powInput not found", TEST_CAPTCHA, "<html>"), FAILURE_PAGE_CHANGED),
        ("script changed", VKCaptchaSolverError(op="captcha script: debug_info not found", changed=True), FAILURE_PAGE_CHANGED),
        ("unknown type", UnknownCaptchaTypeError("puzzle"), FAILURE_PAGE_CHANGED),
        ("network", net_error("captcha page", ConnectionResetError("connection reset")), FAILURE_TIMEOUT),
        ("wrapped deadline", wrapped, FAILURE_TIMEOUT),
        ("raw timeout", asyncio.TimeoutError(), FAILURE_TIMEOUT),
        ("HTTP 503", HTTPError(503, "Service Unavailable", TEST_CAPTCHA), FAILURE_TIMEOUT),
        ("HTTP 403", HTTPError(403, "Forbidden", TEST_CAPTCHA), FAILURE_OTHER),
        ("API error", APIError({"error_code": 100, "error_msg": "One of the parameters"}, "captchaNotRobot.check"), FAILURE_OTHER),
        ("no success token", VKCaptchaSolverError(op="captchaNotRobot.check: no success_token"), FAILURE_OTHER),
        ("anything else", ValueError("x"), FAILURE_OTHER),
        ("none", None, FAILURE_OTHER),
    ]:
        assert failure_kind(err) == want, name


def test_error_texts():
    assert str(APIError({"status": "BOT", "show_captcha_type": "slider", "error_msg": "Bad method status"}, "captchaNotRobot.check")) == (
        "[Status: BOT] (captchaNotRobot.check) Bad method status; show_captcha_type slider"
    )
    assert str(APIError({"error_code": 14, "error_msg": "Captcha needed"}, "m")) == "[Code: 14] (m) Captcha needed"
    e = HTTPError(502, "Bad Gateway", TEST_CAPTCHA)
    assert (e.status, e.status_text, e.url, str(e)) == (502, "Bad Gateway", TEST_CAPTCHA, "HTTP Error 502: Bad Gateway")
    assert str(HTTPError(500, "x", "u", "custom")) == "custom"
    assert str(VKCaptchaSolverError("plain")) == "plain"
    n = net_error("captcha page", OSError("Cannot connect to host https://user:hunter2@proxy:3128/x?session_token=T"))
    assert "hunter2" not in str(n) and "session_token" not in str(n) and str(n).startswith("captcha page: OSError: ")


async def test_failure_of_solve(make_solver):
    from conftest import new_bff_fake, server_context

    async def run(setup, options=None):
        f = new_bff_fake("checkbox")
        setup(f)
        await f.start(server_context())
        try:
            with pytest.raises(Exception) as ei:
                await make_solver(f, options).solve(TEST_CAPTCHA)
            return ei.value
        finally:
            await f.close()

    e = await run(lambda f: f.set("id.vk.com/not_robot_captcha", "<html>nothing</html>"))
    assert failure_kind(e) == FAILURE_PAGE_CHANGED and isinstance(e.report, Report) and e.report.failure == FAILURE_PAGE_CHANGED
    e = await run(lambda f: f.set("captchaNotRobot.check", '{"response":{"status":"BOT"}}'))
    assert failure_kind(e) == FAILURE_BOT and e.report.bff and e.report.kind == "checkbox"
    assert e.report.script == "recorded-43" and e.report.took > 0
    e = await run(lambda f: f.queue("id.vk.com/not_robot_captcha", Reply(hang=True), Reply(hang=True)), {"pageTimeout": 0.1})
    assert failure_kind(e) == FAILURE_TIMEOUT and e.transport


async def test_whole_solving_timeout(make_solver):
    """The whole solving is bounded: a step that never ends is cut at the
    solving's timeout (timeout kind)."""
    from conftest import new_bff_fake, server_context

    f = new_bff_fake("checkbox")
    f.queue("captchaNotRobot.settings", Reply(hang=True))
    await f.start(server_context())
    try:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        with pytest.raises(VKCaptchaSolverError) as ei:
            await make_solver(f, {"timeout": 0.8}).solve(TEST_CAPTCHA)
        assert failure_kind(ei.value) == FAILURE_TIMEOUT
        assert loop.time() - t0 < 2
    finally:
        await f.close()
