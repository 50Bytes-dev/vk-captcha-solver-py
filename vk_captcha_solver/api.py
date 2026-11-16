"""One solving's HTTP session and what it reads from VK's pages (port of
``api.go``).

A session's requests share a cookie jar, as in a browser tab. On a BFF captcha
page the captchaNotRobot.* calls go as Chrome sent them there; the old page's
calls go as the Python original sent them.
"""

import asyncio
import html as html_lib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit

import aiohttp
from multidict import CIMultiDict

from .exceptions import APIError, HTTPError, VKCaptchaSolverError, net_error, page_error
from .jsontree import OBJECT, STRING, ARRAY, JSONValue, jget, js_number, parse_json, to_python
from .profile import DEFAULT_PROFILE, Script

if TYPE_CHECKING:  # pragma: no cover
    from . import CaptchaSolver, Report

log = logging.getLogger("vk_captcha_solver")

# The screen cookie VK's mobile site sets.
REMIXMDEVICE = "1440/900/2/!!-!!!!!!!!/158"
# The browser profile's: its navigator.languages are ru-RU, ru, en-US, en.
ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"
# A response is bounded; VK's captcha script alone is ~1.5 MB.
MAX_BODY_BYTES = 16 << 20
# The captcha of a validation page, before its session token.
VALIDATION_CAPTCHA_URL = "https://id.vk.com/not_robot_captcha?domain=vk.com&session_token="
# How many tries the captcha page gets while the solving has time.
PAGE_TRIES = 2

_NAV_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"

POW_INPUT_RE = re.compile(r'const powInput\s*=\s*"([^"]+)";', re.IGNORECASE)
POW_INPUT_SINGLE_RE = re.compile(r"const powInput\s*=\s*'([^']+)';", re.IGNORECASE)
DIFFICULTY_RE = re.compile(r"const difficulty\s*=\s*(\d+);")
INIT_RE = re.compile(r"window\.init\s*=\s*(\{[\s\S]*?\});\s*(?:window\.lang|</script>|\Z)")
SCRIPT_URL_RE = re.compile(r"src=(\"|')((.)+not_robot_captcha\.js)")
DEBUG_INFO_RE = re.compile(r"debug_info\s*:\s*[\"']([^\"']*)[\"']")
SESSION_TOKEN_RE = re.compile(r"session_token\s*=\s*([^&]+)&")

# The BFF captcha page (proof of work v2) calls its proof of work script with
# the input, the difficulty, the error to report until the work is done, and
# the order of the probes this load runs first:
# }('xYaqzGZN3pOUZg0B',2,'pow_timeout',["globals","sandbox_behavior",...]));
POW_V2_ARGS_RE = re.compile(r"\}\('([^'\\]+)',\s*(\d+),\s*'[^'\\]*',\s*(\[[^\]]*\])")
# Its debug_info is window.vk.brlefapmjnpg.
DEBUG_INFO_V2_RE = re.compile(r'brlefapmjnpg\s*:\s*"([^"]+)"')
# js_engine_oracle reads the stack of this error: its line and column on the
# page go into the telemetry.
ORACLE_RE = re.compile(r"new Error\(['\"]oracle['\"]\)")

# The validation page (error 17) of 2026-11-16 answers the SPA's XHR with JSON
# whose html holds the old form: the captcha widget's iframeSrc, and the form
# the success_token goes to.
IFRAME_SRC_RE = re.compile(r"iframeSrc\s*:\s*(['\"])(.*?)\1")
CAPTCHA_FORM_RE = re.compile(r"<form\b[^>]*\bclass=(['\"])[^'\"]*\bnot_robot_captcha_form\b[^'\"]*\1[^>]*>", re.IGNORECASE)
ACTION_RE = re.compile(r"\baction=(['\"])(.*?)\1", re.IGNORECASE | re.DOTALL)

try:  # aiohttp-socks brings python-socks; their errors are connection failures
    from python_socks import ProxyError as _ProxyError  # type: ignore

    _NET_ERRORS: Tuple[type, ...] = (aiohttp.ClientError, asyncio.TimeoutError, OSError, _ProxyError)
except Exception:  # pragma: no cover
    _NET_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


def where(url: str) -> str:
    """host/path of url, for the log: never its query."""
    u = urlsplit(url)
    return (u.hostname or "") + (u.path or "")


def query_escape(s: str) -> str:
    """Go's url.QueryEscape."""
    return quote_plus(s, safe="")


def form_json(v: Any) -> str:
    """v as compact JSON, as JSON.stringify writes it: numbers as JavaScript
    writes them, non-ASCII and <, >, & as they are."""
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return js_number(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, JSONValue):
        return v.dumps()
    if isinstance(v, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + form_json(x) for k, x in v.items()) + "}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(form_json(x) for x in v) + "]"
    raise TypeError("cannot write %s as JSON" % type(v).__name__)


def form_value(v: Any) -> str:
    """A parameter as the form carries it: None as empty, a string as is,
    anything else as compact JSON."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return form_json(v)


class InitialData:
    """The data of the captcha page's window.init, or, on a BFF page, of
    captchaNotRobot.initSession with the domain and session token of the page
    address."""

    def __init__(
        self,
        domain: str = "",
        session_token: str = "",
        show_captcha_type: str = "",
        captcha_settings: Optional[List[Tuple[str, Optional[JSONValue]]]] = None,
        content_settings: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self.domain = domain
        self.session_token = session_token
        self.show_captcha_type = show_captcha_type
        self.captcha_settings = captcha_settings or []
        # The captcha kinds of initSession: their settings go by key.
        self.content_settings = content_settings or []

    def copy(self) -> "InitialData":
        return InitialData(
            self.domain,
            self.session_token,
            self.show_captcha_type,
            list(self.captcha_settings),
            list(self.content_settings),
        )

    def session_fields(self) -> List[Tuple[str, Any]]:
        """The parameters every captchaNotRobot.* call of a session starts with."""
        return [("session_token", self.session_token), ("domain", self.domain)]

    def settings_of(self, kind: str) -> Any:
        """The captcha_settings entry of the captcha kind: the settings_key of a
        BFF page, else the string as is, any other JSON as its text, and None
        when there is none."""
        for t, key in self.content_settings:
            if t == kind:
                return key
        for t, raw in self.captcha_settings:
            if t != kind:
                continue
            if raw is None or (raw.kind not in (OBJECT, ARRAY, STRING) and raw.text == "null"):
                return None
            if raw.kind == STRING:
                return raw.text
            return raw
        return None


class InitialParams:
    """What the captcha page gives."""

    def __init__(self, data: InitialData, difficulty: int, pow_input: str, page_url: str) -> None:
        self.data = data
        self.difficulty = difficulty
        self.pow_input = pow_input
        # The captcha script that holds debug_info, on the old page; a BFF page
        # (v2) has debug_info itself.
        self.script_url = ""
        self.v2 = False
        self.debug_info = ""
        # The page's address after redirects; nav_start (monotonic s) and
        # nav_start_ms (Unix ms) are when the load of it that came was asked
        # for (performance.timeOrigin); loaded is how long it took to come (s).
        self.page_url = page_url
        self.nav_start = 0.0
        self.nav_start_ms = 0
        self.loaded = 0.0
        # The order of the probes this load of a BFF page runs first, the
        # "line:column" of its oracle error ("" when the page has none the
        # solver finds), and the version of the proof of work script the page
        # runs (Profile.script_for).
        self.probe_order: List[str] = []
        self.stack_pos = ""
        self.script: Optional[Script] = None


def oracle_position(page: str) -> str:
    """ "line:column" of the oracle error on the page as V8 puts it in a stack:
    1-based, the column in UTF-16 code units. "" when the page has none."""
    m = ORACLE_RE.search(page)
    if not m:
        return ""
    before = page[: m.start()]
    line_start = before.rfind("\n") + 1
    column = 1 + sum(2 if ord(c) > 0xFFFF else 1 for c in before[line_start:])
    return "%d:%d" % (before.count("\n") + 1, column)


def _decode(body: bytes, charset: Optional[str]) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _origin(url: str) -> str:
    u = urlsplit(url)
    return "%s://%s" % (u.scheme, u.netloc.rsplit("@", 1)[-1])


def _js_unescape(s: str) -> str:
    """A JavaScript string literal's text: the escapes a URL may hold."""
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r"\\(.)", r"\1", s)


class Session:
    """One solving. Its requests share a cookie jar, as in a browser tab."""

    def __init__(self, solver: "CaptchaSolver", proxy: Optional[str], deadline: float, report: "Report") -> None:
        self.s = solver
        self._proxy = proxy  # never logged nor put in an error text
        self.deadline = deadline  # time.monotonic() the solving must end by
        # v2 is set once the page is a BFF captcha page.
        self.v2 = False
        self.report = report
        self.http: Optional[aiohttp.ClientSession] = None
        self._http_proxy: Optional[str] = None

    async def __aenter__(self) -> "Session":
        connector, self._http_proxy = self.s._connector_for(self._proxy)
        self.http = aiohttp.ClientSession(
            connector=connector,
            cookie_jar=aiohttp.CookieJar(quote_cookie=False),
            timeout=aiohttp.ClientTimeout(total=None),
            auto_decompress=True,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.http is not None:
            await self.http.close()
            self.http = None

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    # -- HTTP ------------------------------------------------------------------

    def _headers(self) -> CIMultiDict:
        """The solver's headers, User-Agent and client hints."""
        h: CIMultiDict = CIMultiDict()
        for k, v in self.s._headers.items():
            if k.lower() != "user-agent":
                h.add(k, v)
        h["User-Agent"] = self.s._user_agent
        if self.s._user_agent == DEFAULT_PROFILE.user_agent:
            for k, v in DEFAULT_PROFILE.client_hints.items():
                h[k] = v
        if "Accept-Language" not in h:
            h["Accept-Language"] = ACCEPT_LANGUAGE
        return h

    async def _do(
        self,
        op: str,
        method: str,
        url: str,
        headers: CIMultiDict,
        data: Optional[bytes] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[bytes, str, Optional[str]]:
        """Send a request and return the body of a response below 400, the URL
        it came from after redirects and its charset."""
        assert self.http is not None
        limit = min(timeout if timeout is not None else self.s._request_timeout, self.remaining())
        if limit <= 0:
            raise net_error(op, asyncio.TimeoutError("no time left for the request"))
        cookies = {"remixmdevice": REMIXMDEVICE}
        cookies.update(self.s._cookies)
        started = time.monotonic()
        try:
            async with self.http.request(
                method,
                url,
                headers=headers,
                data=data,
                cookies=cookies,
                proxy=self._http_proxy,
                timeout=aiohttp.ClientTimeout(total=limit),
                allow_redirects=True,
            ) as resp:
                log.debug("%s %s -> %d in %.2f s", method, where(url), resp.status, time.monotonic() - started)
                if resp.status >= 400:
                    body = await _read(resp, MAX_BODY_BYTES)
                    raise HTTPError(resp.status, resp.reason or "", url, body=_decode(body, resp.charset))
                body = await _read(resp, MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise VKCaptchaSolverError(op=op + ": response is too large")
                return body, str(resp.url), resp.charset
        except VKCaptchaSolverError:
            raise
        except aiohttp.InvalidURL:
            raise VKCaptchaSolverError(op=op + ": bad URL") from None
        except _NET_ERRORS as e:
            log.debug("%s %s failed in %.2f s: %s", method, where(url), time.monotonic() - started, type(e).__name__)
            raise net_error(op, e) from None

    async def get(self, op: str, url: str, timeout: Optional[float] = None) -> Tuple[str, str]:
        """Fetch a page as Chrome opens a page from another site; its text and
        its URL after redirects."""
        h = self._headers()
        h["Accept"] = _NAV_ACCEPT
        h["Sec-Fetch-Dest"] = "document"
        h["Sec-Fetch-Mode"] = "navigate"
        h["Sec-Fetch-Site"] = "cross-site"
        h["Upgrade-Insecure-Requests"] = "1"
        body, final, charset = await self._do(op, "GET", url, h, timeout=timeout)
        return _decode(body, charset), final

    async def get_page(self, op: str, url: str) -> Tuple[str, str, float, int, float]:
        """Load the captcha page as get does, each try within the solver's page
        timeout; the page, its URL, when the try that came was started
        (monotonic s and Unix ms) and how long it took. A try that failed in
        the network or with a 5xx is made once more while the solving has time:
        loading the page changes nothing at VK, and a hung connection was the
        most common reason a captcha went unsolved."""
        attempt = 1
        while True:
            started, started_ms = time.monotonic(), time.time_ns() // 1_000_000
            try:
                page, final = await self.get(op, url, timeout=self.s._page_timeout)
                return page, final, started, started_ms, time.monotonic() - started
            except VKCaptchaSolverError as e:
                if attempt >= PAGE_TRIES or self.remaining() <= 0 or not retryable(e):
                    raise
                log.debug("%s: loading %s again", op, where(url))
                attempt += 1

    async def call(self, method: str, params: Sequence[Tuple[str, Any]]) -> Any:
        """Invoke a captchaNotRobot.* method with a POST form and return its
        response. Lists and objects go as compact JSON, as VK's own script sends
        them.

        On a BFF page the call goes as Chrome sent it there: to the v2 endpoint
        with v in the query, and the form in the widget's order — params as
        given with the solver's adFp after domain (but on initSession), and an
        empty access_token last — from the id.vk.com origin. The old page's
        form goes in key order with v."""
        parts: List[Tuple[str, str]] = []
        values: Dict[str, str] = {}
        for k, v in params:
            try:
                s = form_value(v)
            except (TypeError, ValueError) as e:
                raise VKCaptchaSolverError(op=method + ": parameter " + k, cause=e) from None
            values[k] = s
            parts.append((k, s))
            if self.v2 and k == "domain" and method != "captchaNotRobot.initSession":
                parts.append(("adFp", self.s._ad_fp))
        if self.v2:
            parts.append(("access_token", ""))
            endpoint = self.s._v2_api_base + "/method/" + method + "?v=" + query_escape(self.s._v2_version)
            body = "&".join(query_escape(k) + "=" + query_escape(v) for k, v in parts)
        else:
            values["v"] = self.s._version
            endpoint = self.s._api_base + "/method/" + method
            body = "&".join(query_escape(k) + "=" + query_escape(values[k]) for k in sorted(values))

        h = self._headers()
        h["Content-Type"] = "application/x-www-form-urlencoded"
        if self.v2:
            # A fetch of id.vk.com's script: VK's antibot answers BOT to the
            # same call without these (live check of the Go port, 2026-11-03).
            h["Accept"] = "*/*"
            h["Origin"] = "https://id.vk.com"
            h["Referer"] = "https://id.vk.com/"
            h["Sec-Fetch-Dest"] = "empty"
            h["Sec-Fetch-Mode"] = "cors"
            h["Sec-Fetch-Site"] = "same-site"
            h["Priority"] = "u=1, i"

        raw, _, _ = await self._do(method, "POST", endpoint, h, data=body.encode("ascii"))
        try:
            envelope = json.loads(raw)
        except ValueError:
            raise VKCaptchaSolverError(op=method + ": response is not JSON") from None
        if envelope is not None and not isinstance(envelope, dict):
            raise VKCaptchaSolverError(op=method + ": response is not JSON")
        envelope = envelope or {}
        if envelope.get("error") is not None:
            raise api_error(method, envelope["error"])
        if "response" in envelope:
            resp = envelope["response"]
            if isinstance(resp, dict):
                status, sct = resp.get("status"), resp.get("show_captcha_type")
                if isinstance(status, str) and status not in ("", "OK"):
                    raise APIError(
                        {
                            "status": status,
                            "show_captcha_type": sct if isinstance(sct, str) else "",
                            "error_msg": "Bad method status",
                        },
                        method,
                    )
            return resp
        return envelope

    # -- the captcha page --------------------------------------------------------

    async def initial_params(self, page_url: str) -> InitialParams:
        """Fetch the captcha page and read the proof of work task, window.init
        and the captcha script URL from it, or, on a BFF page, what
        initial_params_v2 reads."""
        op = "captcha page"
        page, final, nav_start, nav_start_ms, loaded = await self.get_page(op, page_url)
        if not POW_INPUT_RE.search(page) and not POW_INPUT_SINGLE_RE.search(page) and POW_V2_ARGS_RE.search(page):
            initial = await self.initial_params_v2(final, page)
            initial.nav_start, initial.nav_start_ms, initial.loaded = nav_start, nav_start_ms, loaded
            return initial

        m = POW_INPUT_RE.search(page) or POW_INPUT_SINGLE_RE.search(page)
        if not m:
            raise page_error(op + ": powInput not found", final, page)
        pow_input = m.group(1)
        m = DIFFICULTY_RE.search(page)
        if not m:
            raise page_error(op + ": difficulty not found", final, page)
        difficulty = int(m.group(1))

        m = INIT_RE.search(page)
        if not m:
            raise page_error(op + ": window.init not found", final, page)
        data = _read_window_init(m.group(1))
        if data is None:
            raise page_error(op + ": invalid window.init data", final, page)
        if not data.session_token:
            raise page_error(op + ": window.init has no session_token", final, page)

        m = SCRIPT_URL_RE.search(page)
        if not m:
            raise page_error(op + ": captcha script url not found", final, page)
        try:
            script_url = urljoin(final, m.group(2))
        except ValueError:
            raise page_error(op + ": bad captcha script url", final, page) from None

        initial = InitialParams(data, difficulty, pow_input, final)
        initial.script_url = script_url
        initial.nav_start, initial.nav_start_ms, initial.loaded = nav_start, nav_start_ms, loaded
        return initial

    async def initial_params_v2(self, page_url: str, page: str) -> InitialParams:
        """Read a BFF captcha page (read_bff_page); the captcha data comes from
        captchaNotRobot.initSession, as the page's own script asks for it."""
        initial = read_bff_page(page_url, page)
        data = initial.data
        self.v2 = True
        resp = await self.call(
            "captchaNotRobot.initSession",
            [("session_token", data.session_token), ("domain", data.domain), ("lang", 0)],
        )
        resp = _object(resp, "captchaNotRobot.initSession")
        kind = resp.get("show_captcha_type") or ""
        content = resp.get("content_settings") or []
        if not isinstance(kind, str) or not isinstance(content, list):
            raise VKCaptchaSolverError(op="captchaNotRobot.initSession: bad response")
        for c in content:
            if not isinstance(c, dict) or not isinstance(c.get("type", ""), str) or not isinstance(
                c.get("settings_key", ""), str
            ):
                raise VKCaptchaSolverError(op="captchaNotRobot.initSession: bad response")
        data.show_captcha_type = kind
        data.content_settings = [(c.get("type", ""), c.get("settings_key", "")) for c in content]

        return initial

    async def debug_info(self, script_url: str) -> str:
        """The debug_info value of VK's captcha script, from the cache when this
        script was read before (its URL names its version)."""
        cached = self.s._cached_debug_info(script_url)
        if cached is not None:
            return cached
        op = "captcha script"
        script, _ = await self.get(op, script_url)
        m = DEBUG_INFO_RE.search(script)
        if not m or not m.group(1):
            raise VKCaptchaSolverError(op=op + ": debug_info not found", url=script_url, changed=True)
        self.s._cache_debug_info(script_url, m.group(1))
        return m.group(1)

    async def settings(self, data: InitialData) -> Tuple[float, List[str]]:
        """captchaNotRobot.settings: sensors_delay and bridge_sensors_list."""
        method = "captchaNotRobot.settings"
        resp = _object(await self.call(method, data.session_fields()), method)
        delay = resp.get("sensors_delay")
        sensors = resp.get("bridge_sensors_list")
        delay = 0 if delay is None else delay
        sensors = [] if sensors is None else sensors
        if isinstance(delay, bool) or not isinstance(delay, (int, float)):
            raise VKCaptchaSolverError(op=method + ": bad response")
        if not isinstance(sensors, list) or not all(isinstance(x, str) for x in sensors):
            raise VKCaptchaSolverError(op=method + ": bad response")
        return float(delay), list(sensors)

    async def content(self, data: InitialData) -> Dict[str, Any]:
        """captchaNotRobot.getContent: the slider picture and its swaps."""
        method = "captchaNotRobot.getContent"
        resp = _object(
            await self.call(
                method, data.session_fields() + [("captcha_settings", data.settings_of(data.show_captcha_type))]
            ),
            method,
        )
        image, steps = resp.get("image") or "", resp.get("steps")
        if steps is None:
            steps = []
        if (
            not isinstance(image, str)
            or not isinstance(steps, list)
            or not all(isinstance(x, int) and not isinstance(x, bool) for x in steps)
        ):
            raise VKCaptchaSolverError(op=method + ": bad response")
        return {"extension": resp.get("extension") or "", "image": image, "steps": steps}

    async def component_done(self, data: InitialData, device: Any) -> None:
        """Report the captcha component loaded in the fake browser."""
        await self.call("captchaNotRobot.componentDone", data.session_fields() + [("device", device)])

    async def component_done_v2(self, data: InitialData, browser_fp: str) -> None:
        """Report the component of a BFF page loaded, with the profile's device
        and browser_fp: empty at the page's start, as Chrome sent it then (its
        fingerprint is not ready yet), the profile's once a slider comes after
        the checkbox."""
        await self.call(
            "captchaNotRobot.componentDone",
            data.session_fields() + [("browser_fp", browser_fp), ("device", DEFAULT_PROFILE.device)],
        )

    async def check(self, params: Sequence[Tuple[str, Any]]) -> str:
        """Send the answer and return the success_token."""
        method = "captchaNotRobot.check"
        resp = await self.call(method, params)
        token = resp.get("success_token") if isinstance(resp, dict) else None
        if not isinstance(token, str):
            token = ""
        if not token:
            raise VKCaptchaSolverError(op=method + ": no success_token")
        return token

    async def end_session(self, data: InitialData) -> None:
        await self.call("captchaNotRobot.endSession", data.session_fields())

    # -- the validation page ------------------------------------------------------

    async def validation_page(self, url: str) -> Tuple[str, str, str]:
        """Read the validation page of error 17 at url: the address of its
        captcha, where its form posts the success_token, and the page's URL
        after redirects. The page goes as m.vk.com's SPA asks for it (an XHR):
        a plain load answers the SPA's shell, which has no captcha. Its cookies
        stay in the session's jar for validate."""
        op = "validation page"
        h = self._headers()
        h["Accept"] = "*/*"
        h["X-Requested-With"] = "XMLHttpRequest"
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-Mode"] = "cors"
        h["Sec-Fetch-Site"] = "same-origin"
        body, final, charset = await self._do(op, "GET", url, h)
        captcha_url, action_url = read_validation_page(_decode(body, charset), final, url)
        log.debug("%s: captcha at %s, the form posts to %s", op, where(captcha_url), where(action_url))
        return captcha_url, action_url, final

    async def validate(self, action_url: str, page_url: str, success_token: str) -> None:
        """Post the success_token to the validation page's form as m.vk.com's
        SPA posts a form: an XHR with _ajax=1. A plain form submit answers the
        SPA's shell and validates nothing; the XHR answers JSON whose location
        is https://oauth.vk.com/blank.html#success=1, and the call that met
        error 17 then goes through (both checked live, 2026-11-16). An answer
        that names no location is VK's change (page_changed); a location
        without success=1 is a validation VK did not take (failed). Either
        carries the answer in .body."""
        op = "validate"
        h = self._headers()
        h["Content-Type"] = "application/x-www-form-urlencoded"
        h["Accept"] = "*/*"
        h["X-Requested-With"] = "XMLHttpRequest"
        h["Origin"] = _origin(page_url)
        h["Referer"] = page_url
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-Mode"] = "cors"
        h["Sec-Fetch-Site"] = "same-origin" if _origin(action_url) == _origin(page_url) else "same-site"
        body = "success_token=" + query_escape(success_token) + "&_ajax=1"
        raw, final, charset = await self._do(op, "POST", action_url, h, data=body.encode("ascii"))
        text = _decode(raw, charset)
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        location = doc.get("location") if isinstance(doc, dict) else None
        if not isinstance(location, str) or not location:
            raise page_error(op + ": the answer names no location", final, text)
        if "success=1" not in location:
            raise VKCaptchaSolverError(op=op + ": VK did not confirm the validation", url=final, body=text)
        log.debug("%s: VK confirmed the validation", op)


def read_bff_page(page_url: str, page: str) -> InitialParams:
    """Read a BFF captcha page at page_url: the proof of work task from the
    arguments of its script, the version of that script (Profile.script_for),
    debug_info from window.vk, and the session token of the page address. A
    page that lacks one, or whose probes are not the profile's, is VK's change
    (page_changed), found before any captchaNotRobot call."""
    op = "captcha page"
    m = POW_V2_ARGS_RE.search(page)
    if m is None:
        raise page_error(op + ": proof of work v2 not found", page_url, page)
    difficulty = int(m.group(2))
    try:
        order = json.loads(m.group(3))
    except ValueError:
        order = None
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        raise page_error(op + ": bad probe order", page_url, page)
    # The proof of work script is the <script> that ends with this call.
    src = page[: m.end()]
    src = src[max(src.rfind("<script"), 0) :]
    try:
        script = DEFAULT_PROFILE.script_for(order, src)
    except ValueError as e:
        raise page_error(op + ": " + str(e), page_url, page) from None
    d = DEBUG_INFO_V2_RE.search(page)
    if not d:
        raise page_error(op + ": brlefapmjnpg (debug_info) not found", page_url, page)
    q = parse_qs(urlsplit(page_url).query, keep_blank_values=True)
    data = InitialData(
        domain=(q.get("domain") or [""])[0],
        session_token=(q.get("session_token") or [""])[0],
    )
    if not data.session_token:
        raise page_error(op + ": the address has no session_token", page_url, None)
    initial = InitialParams(data, difficulty, m.group(1), page_url)
    initial.v2 = True
    initial.debug_info = d.group(1)
    initial.probe_order = order
    initial.stack_pos = oracle_position(page)
    initial.script = script
    return initial


def read_validation_page(text: str, page_url: str, request_url: str) -> Tuple[str, str]:
    """The captcha address and the form action of a validation page that came
    from page_url (after redirects) for request_url. The body is m.vk.com's
    XHR answer (JSON whose html holds the page) or the page itself (the old
    server-rendered form). The captcha address is the widget's iframeSrc as the
    page gives it, else the one built from the page's session_token; the
    action is the not_robot_captcha_form's, resolved against the page, else
    request_url. A page with no captcha address is page_changed."""
    op = "validation page"
    page = text
    try:
        doc = json.loads(text)
    except ValueError:
        doc = None
    if isinstance(doc, dict) and isinstance(doc.get("html"), str):
        page = doc["html"]

    m = IFRAME_SRC_RE.search(page)
    if m and m.group(2).strip():
        captcha_url = urljoin(page_url, _js_unescape(m.group(2).strip()))
    else:
        t = SESSION_TOKEN_RE.search(page) or SESSION_TOKEN_RE.search(text)
        if not t:
            raise page_error(op + ": captcha address not found", page_url, text)
        captcha_url = VALIDATION_CAPTCHA_URL + t.group(1) + "&variant=popup&blank=1"

    action_url = request_url
    form = CAPTCHA_FORM_RE.search(page)
    if form:
        a = ACTION_RE.search(form.group(0))
        if a and a.group(2).strip():
            action_url = urljoin(page_url, html_lib.unescape(a.group(2).strip()))
    return captcha_url, action_url


def retryable(err: BaseException) -> bool:
    """A failure worth one more try: the connection failed or timed out, or VK
    answered 5xx."""
    if isinstance(err, HTTPError):
        return err.status >= 500
    if isinstance(err, APIError):
        return False
    return isinstance(err, VKCaptchaSolverError) and err.transport


def api_error(method: str, raw: Any) -> APIError:
    """A method's error object."""
    if not isinstance(raw, dict):
        return APIError({"error_msg": "unreadable error"}, method)
    code, msg, status = raw.get("error_code"), raw.get("error_msg"), raw.get("status")
    if (
        (code is not None and (isinstance(code, bool) or not isinstance(code, int)))
        or (msg is not None and not isinstance(msg, str))
        or (status is not None and not isinstance(status, str))
    ):
        return APIError({"error_msg": "unreadable error"}, method)
    return APIError({"error_code": code or 0, "error_msg": msg or "Unknown error", "status": status or ""}, method)


def _object(resp: Any, method: str) -> Dict[str, Any]:
    if resp is None:
        return {}
    if not isinstance(resp, dict):
        raise VKCaptchaSolverError(op=method + ": bad response")
    return resp


def _read_window_init(text: str) -> Optional[InitialData]:
    try:
        tree = parse_json(text)
    except ValueError:
        return None
    data = jget(tree, "data")
    if data is None or data.kind != OBJECT:
        return None

    def string(key: str) -> Optional[str]:
        v = jget(data, key)
        if v is None or (v.kind not in (OBJECT, ARRAY, STRING) and v.text == "null"):
            return ""
        return v.text if v.kind == STRING else None

    domain, token, kind = string("domain"), string("session_token"), string("show_captcha_type")
    if domain is None or token is None or kind is None:
        return None
    settings: List[Tuple[str, Optional[JSONValue]]] = []
    raw = jget(data, "captcha_settings")
    if raw is not None and raw.kind == ARRAY:
        for item in raw.items:
            if item.kind != OBJECT:
                return None
            t = jget(item, "type")
            if t is not None and t.kind != STRING:
                return None
            settings.append((t.text if t is not None else "", jget(item, "settings")))
    elif raw is not None and to_python(raw) is not None:
        return None
    return InitialData(domain, token, kind, settings)


async def _read(resp: aiohttp.ClientResponse, limit: int) -> bytes:
    """Up to limit bytes of the body."""
    chunks: List[bytes] = []
    size = 0
    while size < limit:
        chunk = await resp.content.read(min(65536, limit - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)
