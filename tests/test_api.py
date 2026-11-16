"""The public API, concurrency, proxies and what the log says."""

import asyncio
import base64
import logging
import re

import pytest

import vk_captcha_solver
from conftest import TEST_CAPTCHA, TEST_SESSION_TOKEN, FakeCall, Reply
from vk_captcha_solver import CaptchaSolver, VKCaptchaSolverError, failure_kind


def test_exports():
    for name in ("CaptchaSolver", "VKCaptchaSolverError", "APIError", "HTTPError", "IApiOptions"):
        assert name in vk_captcha_solver.__all__ and hasattr(vk_captcha_solver, name)
    assert vk_captcha_solver.__version__ == "0.2.0"
    assert vk_captcha_solver.FAILURE_TIMEOUT == "timeout" and vk_captcha_solver.FAILURE_OTHER == "failed"


async def test_old_usage_still_works(bff, make_solver):
    s = make_solver(bff)
    async with s.session(proxy=None) as same:
        assert same is s
        assert await same.solve(TEST_CAPTCHA) == "SUCCESS-TOKEN"
    async with s:
        assert await s.solve(TEST_CAPTCHA) == "SUCCESS-TOKEN"

    class CaptchaError:
        redirect_uri = TEST_CAPTCHA

    # vkbottle calls handler(error, current_proxy).
    assert await s.vkbottle_captcha_handler(CaptchaError(), None) == "SUCCESS-TOKEN"


async def test_session_proxy_is_per_task():
    s = CaptchaSolver({"proxy": "http://default:1"})
    seen = {}

    async def task(name, proxy):
        async with s.session(proxy=proxy):
            await asyncio.sleep(0.01)
            seen[name] = s._proxy_of(None)
            assert s._proxy_of("http://explicit:3") == "http://explicit:3"

    await asyncio.gather(task("a", "http://a:1"), task("b", "http://b:2"), task("none", None))
    assert seen == {"a": "http://a:1", "b": "http://b:2", "none": "http://default:1"}
    assert s._proxy_of(None) == "http://default:1"


async def test_concurrent_solvings_do_not_share_cookies(make_solver):
    """Two solvings on one solver at once: each has its own cookie jar."""
    from conftest import new_bff_fake, server_context

    f = new_bff_fake("checkbox")
    page = f.pages["id.vk.com/not_robot_captcha"]

    def page_reply(call: FakeCall) -> Reply:
        token = call.q("session_token")
        return Reply(body=page, headers=[("Set-Cookie", "sid=%s; Path=/; Domain=vk.com; Secure" % token)])

    f.hooks["id.vk.com/not_robot_captcha"] = page_reply
    await f.start(server_context())
    try:
        s = make_solver(f)

        async def slow_wait(until):
            await asyncio.sleep(0.05)  # the two solvings interleave here

        s._wait = slow_wait
        urls = [TEST_CAPTCHA.replace(TEST_SESSION_TOKEN, t) for t in ("TOKEN-A", "TOKEN-B")]
        tokens = await asyncio.gather(*(s.solve(u) for u in urls))
        assert tokens == ["SUCCESS-TOKEN", "SUCCESS-TOKEN"]
        api = [c for c in f.calls if c.host == "api.vk.com"]
        assert len(api) == 10
        for c in api:
            assert c.cookies.get("sid") == c.f("session_token"), c
        order = [c.f("session_token") for c in api]
        assert order != sorted(order), "the solvings did not overlap"
    finally:
        await f.close()


class ConnectProxy:
    """An HTTP proxy that tunnels CONNECT to the fake VK."""

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.connects = []
        self.auth = []
        self.server = None
        self.port = 0

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        self.connects.append(lines[0])
        for line in lines[1:]:
            if line.lower().startswith("proxy-authorization:"):
                self.auth.append(line.split(":", 1)[1].strip())
        r2, w2 = await asyncio.open_connection("127.0.0.1", self.target_port)
        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await writer.drain()

        async def pipe(r, w):
            try:
                while True:
                    data = await r.read(65536)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                w.close()

        await asyncio.gather(pipe(reader, w2), pipe(r2, writer))

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


async def test_solve_through_http_proxy_and_no_proxy_in_texts(bff, make_solver, caplog):
    caplog.set_level(logging.DEBUG, logger="vk_captcha_solver")
    proxy = ConnectProxy(bff.port)
    await proxy.start()
    try:
        s = make_solver(bff)
        url = "http://user:hunter2@127.0.0.1:%d" % proxy.port
        assert await s.solve(TEST_CAPTCHA, proxy=url) == "SUCCESS-TOKEN"
        assert proxy.connects and all(c.startswith("CONNECT ") for c in proxy.connects)
        assert any(c.startswith("CONNECT id.vk.com:443") for c in proxy.connects)
        assert any(c.startswith("CONNECT api.vk.com:443") for c in proxy.connects)
        assert proxy.auth and base64.b64decode(proxy.auth[0].split()[1]) == b"user:hunter2"
    finally:
        await proxy.close()
    # A proxy that is gone: the error and the log keep its password to themselves.
    with pytest.raises(VKCaptchaSolverError) as ei:
        await make_solver(bff, {"pageTimeout": 1}).solve(TEST_CAPTCHA, proxy="http://user:hunter2@127.0.0.1:1", timeout=5)
    assert failure_kind(ei.value) == "timeout"
    assert "hunter2" not in str(ei.value) and "hunter2" not in caplog.text
    for bad in ("127.0.0.1:8080", "ftp://user:hunter2@host:21", "http://"):
        with pytest.raises(VKCaptchaSolverError) as ei:
            await make_solver(bff).solve(TEST_CAPTCHA, proxy=bad)
        assert "hunter2" not in str(ei.value)
    assert "hunter2" not in caplog.text


async def test_log_lines(bff, make_solver, caplog):
    """One INFO line for a solved captcha, one WARNING for a failed one; no
    token, hash or query in any line."""
    from conftest import new_bff_fake, server_context

    caplog.set_level(logging.DEBUG, logger="vk_captcha_solver")
    await make_solver(bff).solve(TEST_CAPTCHA)
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    msg = infos[0].getMessage()
    assert msg.startswith("VK captcha solved: page=bff kind=checkbox script=recorded-43 warm=")
    assert re.search(r"check_at=\d+\.\d\ds took=\d+\.\d\ds$", msg)
    assert any("GET id.vk.com/not_robot_captcha -> 200" in r.getMessage() for r in caplog.records)

    caplog.clear()
    bff.set("captchaNotRobot.check", '{"response":{"status":"BOT"}}')
    with pytest.raises(VKCaptchaSolverError):
        await make_solver(bff).solve(TEST_CAPTCHA)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage().startswith("VK captcha not solved (bot): [Status: BOT] (captchaNotRobot.check)")

    from test_validation import VALIDATE, activation_reply

    v = new_bff_fake("checkbox")
    v.hooks["m.vk.com/activation"] = activation_reply
    await v.start(server_context())
    try:
        await make_solver(v).solve_validation(VALIDATE)
    finally:
        await v.close()
    for r in caplog.records:
        text = r.getMessage()
        for secret in (TEST_SESSION_TOKEN, "VALIDATION-TOKEN", "SUCCESS-TOKEN", "v2.", "?", "H2", "hunter2"):
            assert secret not in text, (secret, text)
