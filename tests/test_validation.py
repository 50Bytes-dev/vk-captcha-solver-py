"""The validation page of error 17 (not in the Go port: found live on
2026-11-16). m.vk.com answers a plain load of it with the SPA's shell and the
SPA's XHR with JSON whose html holds the captcha widget and its form."""

import json
from urllib.parse import parse_qs

import pytest

from conftest import TEST_DEBUG_INFO, FakeCall, Reply, new_bff_fake, server_context
from vk_captcha_solver import FAILURE_PAGE_CHANGED, VKCaptchaSolverError, failure_kind

VALIDATE = "https://m.vk.com/activation?act=validate&api_hash=H1&hash=H2"
ACTIVATION = "m.vk.com/activation"
CAPTCHA = "id.vk.com/not_robot_captcha"
STL = ("Set-Cookie", "remixstlid=STL; Path=/; Domain=vk.com; Secure")


def xhr_html(token: str = "VALIDATION-TOKEN", action: str = "/activation?act=captcha&amp;api_hash=H1&amp;hash=H2&amp;form=1") -> str:
    """The html of the XHR answer, shaped like m.vk.com's of 2026-11-16."""
    return (
        '<div class="basis"><a class="header__back" href="/activation?act=validate&amp;api_hash=H1&amp;hash=H2">Назад</a>'
        '<div class="PageBlock">\n'
        '  <form class="not_robot_captcha_form" action="' + action + '" method="post" hidden>\n'
        '  <input class="not_robot_captcha_token_input" type="hidden" name="success_token" />\n'
        "</form>\n"
        '<script type="text/javascript" language="javascript" src="https://static.vk.com/captchaSDK/loader/1/umd/index.js"></script>\n'
        "<script>\n  (function() {\n    const initNotRobotCaptcha = async () => {\n"
        "      const { CaptchaWidget } = await window.vkidCaptcha;\n      const captchaWidget = new CaptchaWidget();\n"
        "      try {\n        const successToken = await captchaWidget.show({\n          container: document.body,\n"
        "          iframeSrc: 'https://id.vk.com/not_robot_captcha?domain=vk.com&session_token=" + token + "&variant=popup&blank=1',\n"
        "          captchaType: 'type_1',\n          view: 'popup',\n        });\n"
        "        document.querySelector('.not_robot_captcha_form').submit();\n      } catch (error) {}\n    };\n"
        "    initNotRobotCaptcha();\n  })();\n</script></div>"
        '<a class="fm_item" href="/settings?act=change_regional&api_hash=OTHER&hash=OTHER">English</a>'
    )


SPA_SHELL = '<!DOCTYPE html><html><body><script>window.init({"module":"activation"});</script></body></html>'
# What m.vk.com answers to the form posted as its SPA posts it (live, 2026-11-16).
VALIDATED = '{"location":"https:\\/\\/oauth.vk.com\\/blank.html#success=1","hard":0,"frame":0,"type":1,"notifications":[]}'
JSON_TYPE = ("Content-Type", "application/json; charset=utf-8")


def post_reply(call: FakeCall) -> Reply:
    """A form posted as a plain submit gets the SPA's shell and validates
    nothing; the SPA's own XHR is answered."""
    if call.headers.get("X-Requested-With") != "XMLHttpRequest" or call.f("_ajax") != "1":
        return Reply(body=SPA_SHELL, headers=[("Content-Type", "text/html")])
    return Reply(body=VALIDATED, headers=[JSON_TYPE])


def activation_reply(call: FakeCall) -> Reply:
    if call.http_method == "POST":
        return post_reply(call)
    if call.headers.get("X-Requested-With") != "XMLHttpRequest":
        return Reply(body=SPA_SHELL, headers=[STL])  # what a plain load gets now
    doc = {"title": "Подтверждение", "html": xhr_html(), "js": "", "statsMeta": {}, "static_files": [], "env": {}}
    return Reply(body=json.dumps(doc, ensure_ascii=False), headers=[STL, JSON_TYPE])


@pytest.fixture
async def vfake():
    f = new_bff_fake("checkbox")
    f.hooks[ACTIVATION] = activation_reply
    await f.start(server_context())
    yield f
    await f.close()


async def test_validation_xhr_json_page(vfake, make_solver):
    report = await make_solver(vfake).solve_validation_report(VALIDATE)
    keys = vfake.call_keys()
    assert keys[0] == ACTIVATION and keys[1] == CAPTCHA and keys[-1] == ACTIVATION
    assert keys[2:-1] == [
        "captchaNotRobot.initSession",
        "captchaNotRobot.settings",
        "captchaNotRobot.componentDone",
        "captchaNotRobot.check",
        "captchaNotRobot.endSession",
    ]
    open_, page, post = vfake.calls[0], vfake.calls[1], vfake.calls[-1]
    assert open_.http_method == "GET" and open_.q("act") == "captcha" and open_.q("hash") == "H2"
    assert open_.headers["X-Requested-With"] == "XMLHttpRequest"
    assert page.q("session_token") == "VALIDATION-TOKEN" and page.q("variant") == "popup"
    assert page.cookies.get("remixstlid") == "STL"
    check = vfake.call("captchaNotRobot.check")
    assert check.f("session_token") == "VALIDATION-TOKEN" and check.f("debug_info") == TEST_DEBUG_INFO
    # The success_token goes to the form's action, as the SPA posts a form.
    assert post.http_method == "POST" and post.path == "/activation"
    assert post.q("act") == "captcha" and post.q("api_hash") == "H1" and post.q("hash") == "H2" and post.q("form") == "1"
    assert post.raw_body == "success_token=SUCCESS-TOKEN&_ajax=1"
    assert post.headers["X-Requested-With"] == "XMLHttpRequest" and post.headers["Accept"] == "*/*"
    assert post.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert post.headers["Origin"] == "https://m.vk.com"
    assert post.headers["Referer"] == "https://m.vk.com/activation?act=captcha&api_hash=H1&hash=H2"
    assert post.headers["Sec-Fetch-Mode"] == "cors" and post.headers["Sec-Fetch-Site"] == "same-origin"
    assert post.cookies.get("remixstlid") == "STL"
    assert report.bff and report.kind == "checkbox"


async def test_validation_old_server_rendered_page(make_solver):
    """The old page: the form itself, no iframeSrc; the captcha address is
    built from its session_token and the token goes to the validation page."""
    f = new_bff_fake("checkbox")
    old_page = '<html><script>var u = "/x?session_token=VALIDATION-TOKEN&act=1";</script></html>'
    f.hooks[ACTIVATION] = lambda call: post_reply(call) if call.http_method == "POST" else Reply(body=old_page, headers=[STL])
    await f.start(server_context())
    try:
        await make_solver(f).solve_validation(VALIDATE)
        page, post = f.calls[1], f.calls[-1]
        assert page.key() == CAPTCHA and page.q("session_token") == "VALIDATION-TOKEN" and page.q("blank") == "1"
        assert page.cookies.get("remixstlid") == "STL"
        assert post.http_method == "POST" and post.key() == ACTIVATION
        assert post.q("act") == "captcha" and post.q("hash") == "H2" and post.q("form") is None
        assert parse_qs(post.raw_body) == {"success_token": ["SUCCESS-TOKEN"], "_ajax": ["1"]}
        assert post.cookies.get("remixstlid") == "STL"
    finally:
        await f.close()


async def test_validation_page_without_captcha_address(make_solver):
    f = new_bff_fake("checkbox")
    f.set(ACTIVATION, SPA_SHELL)
    await f.start(server_context())
    try:
        with pytest.raises(VKCaptchaSolverError) as ei:
            await make_solver(f).solve_validation(VALIDATE)
        e = ei.value
        assert failure_kind(e) == FAILURE_PAGE_CHANGED
        assert str(e) == "validation page: captcha address not found"
        assert e.url == "https://m.vk.com/activation?act=captcha&api_hash=H1&hash=H2" and e.body == SPA_SHELL
        assert f.call_keys() == [ACTIVATION]
        assert e.report is not None and e.report.failure == FAILURE_PAGE_CHANGED
    finally:
        await f.close()


@pytest.mark.parametrize(
    "answer, kind, text",
    [
        # the SPA's shell: what a form that validated nothing gets
        (SPA_SHELL, FAILURE_PAGE_CHANGED, "validate: the answer names no location"),
        ('{"html":"<div></div>"}', FAILURE_PAGE_CHANGED, "validate: the answer names no location"),
        ('{"location":"https:\\/\\/oauth.vk.com\\/blank.html#fail=1"}', "failed", "validate: VK did not confirm the validation"),
    ],
    ids=["shell", "no-location", "not-success"],
)
async def test_validation_not_confirmed(make_solver, answer, kind, text):
    """The captcha was solved, but VK's answer to the form does not say the
    validation passed: an error with the answer kept, not a silent success."""
    f = new_bff_fake("checkbox")
    f.hooks[ACTIVATION] = lambda call: Reply(body=answer) if call.http_method == "POST" else activation_reply(call)
    await f.start(server_context())
    try:
        with pytest.raises(VKCaptchaSolverError) as ei:
            await make_solver(f).solve_validation(VALIDATE)
        e = ei.value
        assert failure_kind(e) == kind and str(e) == text
        assert e.body == answer and e.url.startswith("https://m.vk.com/activation?")
        assert f.calls[-1].http_method == "POST" and f.call("captchaNotRobot.check")
    finally:
        await f.close()


async def test_vkbottle_validation_handler(vfake, make_solver):
    class ValidationError:
        redirect_uri = VALIDATE

    await make_solver(vfake).vkbottle_validation_handler(ValidationError(), None)
    assert vfake.calls[-1].http_method == "POST" and vfake.calls[-1].f("success_token") == "SUCCESS-TOKEN"


def test_read_validation_page_variants():
    from vk_captcha_solver.api import read_validation_page

    page = "https://m.vk.com/activation?act=captcha&api_hash=H1&hash=H2"
    doc = json.dumps({"html": xhr_html(token="T1"), "js": ""})
    cap, action = read_validation_page(doc, page, page)
    assert cap == "https://id.vk.com/not_robot_captcha?domain=vk.com&session_token=T1&variant=popup&blank=1"
    assert action == "https://m.vk.com/activation?act=captcha&api_hash=H1&hash=H2&form=1"
    # The JSON answer of an old page: no iframeSrc, a session_token in its html.
    doc = json.dumps({"html": "<a href='/x?session_token=T2&a=1'>", "js": ""})
    cap, action = read_validation_page(doc, page, page)
    assert cap.endswith("session_token=T2&variant=popup&blank=1") and action == page
    # A JSON answer that is not a page object is the page itself.
    cap, _ = read_validation_page('["iframeSrc: \'/c?session_token=T3\'"]', page, page)
    assert cap == "https://m.vk.com/c?session_token=T3"
    with pytest.raises(VKCaptchaSolverError) as ei:
        read_validation_page(json.dumps({"html": "<div></div>"}), page, page)
    assert failure_kind(ei.value) == FAILURE_PAGE_CHANGED and str(ei.value) == "validation page: captcha address not found"
