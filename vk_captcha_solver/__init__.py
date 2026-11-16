"""
VK Captcha Solver: solves the VK ID captcha ("Я не робот") — the page VK links
in redirect_uri of API error 14, and the captcha behind the validation page of
error 17. Both the BFF captcha page (proof of work v2) and the old one.

Example:
    solver = CaptchaSolver()

    token = await solver.solve(redirect_uri, proxy="http://proxy:8080")

    async with solver.session(proxy="http://proxy:8080"):
        token = await solver.solve(redirect_uri)

    # vkbottle
    api.add_captcha_handler(solver.vkbottle_captcha_handler)
    api.add_validation_handler(solver.vkbottle_validation_handler)
"""

import asyncio
import contextvars
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Dict, Optional, Tuple, TypeVar
from urllib.parse import urlsplit

import aiohttp

from .api import Session
from .exceptions import (
    FAILURE_BOT,
    FAILURE_OTHER,
    FAILURE_PAGE_CHANGED,
    FAILURE_TIMEOUT,
    APIError,
    HTTPError,
    UnknownCaptchaTypeError,
    VKCaptchaSolverError,
    failure_kind,
)
from .profile import DEFAULT_PROFILE, process_ad_fingerprint, resolve_timezone
from .solver import solve as _solve
from .solver import wait_until
from .types import IApiOptions

if TYPE_CHECKING:  # pragma: no cover
    from vkbottle import CaptchaError, ValidationError

__version__ = "0.2.0"

log = logging.getLogger("vk_captcha_solver")

#: The endpoint and API version of the old captcha page's calls.
DEFAULT_API_BASE = "https://api.vk.ru"
DEFAULT_API_VERSION = "5.199"
#: The browser the requests pretend to be: the browser profile's, with its
#: client hints.
DEFAULT_USER_AGENT = DEFAULT_PROFILE.user_agent
#: Where and how a BFF captcha page calls the captchaNotRobot.* methods, as
#: Chrome did on 2026-11-03.
V2_API_BASE = "https://api.vk.com"
V2_API_VERSION = "5.131"

DEFAULT_TIMEOUT = 60.0
DEFAULT_PAGE_TIMEOUT = 10.0
DEFAULT_REQUEST_TIMEOUT = 30.0
# The backstop past a solving's own deadline: the steps bound themselves by it.
_GRACE = 1.0

_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")

T = TypeVar("T")


@dataclass
class Report:
    """How a solving went, as far as it got."""

    #: The captcha VK showed first: checkbox or slider.
    kind: str = ""
    #: VK answered the checkbox BOT and offered a slider, and the solver went on.
    slider_after_bot: bool = False
    #: The telemetry was a browser's that had run the page before.
    warm: bool = False
    #: When the last check went, seconds from the page's start (BFF page).
    check_at: float = 0.0
    #: The version of the page's proof of work script the telemetry was made for.
    script: str = ""
    #: The page was a BFF captcha page (proof of work v2).
    bff: bool = False
    #: How long the solving took, seconds.
    took: float = 0.0
    #: The failure kind (failure_kind), "" when solved.
    failure: str = ""

    def describe(self) -> str:
        parts = ["page=%s" % ("bff" if self.bff else "old"), "kind=%s" % (self.kind or "-")]
        if self.bff:
            parts += [
                "script=%s" % (self.script or "-"),
                "warm=%s" % ("yes" if self.warm else "no"),
                "slider_after_bot=%s" % ("yes" if self.slider_after_bot else "no"),
                "check_at=%.2fs" % self.check_at,
            ]
        parts.append("took=%.2fs" % self.took)
        return " ".join(parts)


class CaptchaSolver:
    """Solves VK captchas. Safe for concurrent solvings: every solving runs in
    its own session with its own cookie jar.

    api_options (IApiOptions, every key optional): version, baseUrl (the old
    page's API), headers, cookies, userAgent, proxy, timeout (s, whole solving,
    default 60), pageTimeout (s, one try at the captcha page, default 10; two
    tries), requestTimeout (s, one request, default 30), timezone (IANA name).
    """

    def __init__(self, api_options: Optional[IApiOptions] = None) -> None:
        options: Dict[str, Any] = dict(api_options or {})
        self.api_options = options
        self._api_base = str(options.get("baseUrl") or DEFAULT_API_BASE).rstrip("/")
        self._version = str(options.get("version") or DEFAULT_API_VERSION)
        self._user_agent = str(options.get("userAgent") or DEFAULT_USER_AGENT)
        self._headers: Dict[str, str] = dict(options.get("headers") or {})
        self._cookies: Dict[str, str] = dict(options.get("cookies") or {})
        self._default_proxy: Optional[str] = options.get("proxy") or None
        self._timeout = _positive(options.get("timeout"), DEFAULT_TIMEOUT, "timeout")
        self._page_timeout = _positive(options.get("pageTimeout"), DEFAULT_PAGE_TIMEOUT, "pageTimeout")
        self._request_timeout = _positive(options.get("requestTimeout"), DEFAULT_REQUEST_TIMEOUT, "requestTimeout")
        self._timezone: Optional[str] = options.get("timezone") or None
        self._v2_api_base = V2_API_BASE
        self._v2_version = V2_API_VERSION
        # The ad fingerprint the BFF page sends: one per process, as a browser
        # keeps its window.rb_sync.id.
        self._ad_fp = process_ad_fingerprint()
        # Waits until a monotonic time; tests skip it.
        self._wait: Callable[[float], Awaitable[None]] = wait_until
        # Makes the connector of a solving without a socks proxy; tests set it.
        self._connector_factory: Optional[Callable[[], aiohttp.BaseConnector]] = None
        # debug_info by captcha script URL: the URL names the script's version,
        # and the script is about 1.5 MB (a browser caches it too).
        self._debug_infos: Dict[str, str] = {}
        self._lock = threading.Lock()
        # The proxy of session(): per task, so concurrent sessions do not mix.
        self._session_proxy: contextvars.ContextVar = contextvars.ContextVar(
            "vk_captcha_solver_session_%x" % id(self), default=None
        )

    # =========================================================================
    # Public API
    # =========================================================================

    @asynccontextmanager
    async def session(self, proxy: Optional[str] = None) -> AsyncIterator["CaptchaSolver"]:
        """The solvings in this block (of this task) go through proxy.

        Example:
            >>> async with solver.session(proxy="http://proxy:8080") as s:
            ...     token = await s.solve(redirect_uri)
        """
        token = self._session_proxy.set((proxy,))
        try:
            yield self
        finally:
            self._reset_session(token)

    async def solve(self, redirect_uri: str, proxy: Optional[str] = None, timeout: Optional[float] = None) -> str:
        """Solve the captcha at redirect_uri (redirect_uri of VK error 14) and
        return its success_token. proxy: of this solving (else the session's,
        else the option's); timeout: seconds the whole solving may take.

        Raises VKCaptchaSolverError (failure_kind tells its kind; .report tells
        how far it got)."""
        token, _ = await self.solve_report(redirect_uri, proxy=proxy, timeout=timeout)
        return token

    async def solve_report(
        self, redirect_uri: str, proxy: Optional[str] = None, timeout: Optional[float] = None
    ) -> Tuple[str, Report]:
        """solve, with how it went."""
        return await self._run("captcha", lambda ss: _solve(ss, redirect_uri), proxy, timeout)

    async def solve_validation(
        self, redirect_uri: str, proxy: Optional[str] = None, timeout: Optional[float] = None
    ) -> None:
        """Pass the validation page at redirect_uri (redirect_uri of VK error
        17): open it as m.vk.com's page asks for it, solve the captcha behind it
        and post the success_token to the page's form."""
        await self.solve_validation_report(redirect_uri, proxy=proxy, timeout=timeout)

    async def solve_validation_report(
        self, redirect_uri: str, proxy: Optional[str] = None, timeout: Optional[float] = None
    ) -> Report:
        """solve_validation, with how it went."""

        async def run(ss: Session) -> None:
            validation_uri = redirect_uri.replace("act=validate", "act=captcha")
            captcha_url, action_url, page_url = await ss.validation_page(validation_uri)
            token = await _solve(ss, captcha_url)
            await ss.validate(action_url, page_url, token)

        _, report = await self._run("validation", run, proxy, timeout)
        return report

    # =========================================================================
    # vkbottle Integration
    # =========================================================================

    async def vkbottle_captcha_handler(self, error: "CaptchaError", proxy: Optional[str] = None) -> str:
        """vkbottle's captcha handler: the success_token of error.redirect_uri."""
        return await self.solve(error.redirect_uri, proxy=proxy)

    async def vkbottle_validation_handler(self, error: "ValidationError", proxy: Optional[str] = None) -> None:
        """vkbottle's validation handler: passes error.redirect_uri."""
        await self.solve_validation(error.redirect_uri, proxy=proxy)

    # =========================================================================
    # Legacy context manager
    # =========================================================================

    async def __aenter__(self) -> "CaptchaSolver":
        """Deprecated: solve() needs no session; use solver.session(proxy) for a proxy."""
        self._legacy_tokens = getattr(self, "_legacy_tokens", [])
        self._legacy_tokens.append(self._session_proxy.set((None,)))
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        tokens = getattr(self, "_legacy_tokens", [])
        if tokens:
            self._reset_session(tokens.pop())

    # =========================================================================
    # Private
    # =========================================================================

    def _reset_session(self, token: contextvars.Token) -> None:
        try:
            self._session_proxy.reset(token)
        except ValueError:  # left in another context than it was entered
            self._session_proxy.set(None)

    def _proxy_of(self, proxy: Optional[str]) -> Optional[str]:
        if proxy:
            return proxy
        box = self._session_proxy.get()
        if box is not None and box[0]:
            return box[0]
        return self._default_proxy

    def _timezone_pair(self) -> Tuple[str, int]:
        return resolve_timezone(self._timezone)

    def _connector_for(self, proxy: Optional[str]) -> Tuple[aiohttp.BaseConnector, Optional[str]]:
        """The connector of a solving through proxy, and the proxy aiohttp
        takes per request (http, https). The proxy URL is not echoed: it may
        hold a password."""
        if proxy:
            try:
                u = urlsplit(proxy)
                ok = bool(u.hostname)
                u.port  # raises on a bad port
            except ValueError:
                ok = False
            if not ok:
                raise VKCaptchaSolverError("bad proxy URL")
            scheme = u.scheme.lower()
            if scheme not in _PROXY_SCHEMES:
                raise VKCaptchaSolverError("proxy scheme must be http, https, socks5 or socks5h")
            if scheme.startswith("socks"):
                from aiohttp_socks import ProxyConnector

                url = "socks5" + proxy[len(scheme) :]
                return ProxyConnector.from_url(url, rdns=scheme == "socks5h"), None
            return self._new_connector(), proxy
        return self._new_connector(), None

    def _new_connector(self) -> aiohttp.BaseConnector:
        if self._connector_factory is not None:
            return self._connector_factory()
        return aiohttp.TCPConnector()

    def _cached_debug_info(self, script_url: str) -> Optional[str]:
        with self._lock:
            return self._debug_infos.get(script_url)

    def _cache_debug_info(self, script_url: str, value: str) -> None:
        with self._lock:
            if len(self._debug_infos) >= _MAX_CACHED_SCRIPTS:  # a full cache starts over
                self._debug_infos = {}
            self._debug_infos[script_url] = value

    async def _run(
        self,
        what: str,
        fn: Callable[[Session], Awaitable[T]],
        proxy: Optional[str],
        timeout: Optional[float],
    ) -> Tuple[T, Report]:
        limit = self._timeout if timeout is None else _positive(timeout, self._timeout, "timeout")
        started = time.monotonic()
        report = Report()
        ss = Session(self, self._proxy_of(proxy), started + limit, report)
        try:
            async with ss:
                try:
                    result = await asyncio.wait_for(fn(ss), limit + _GRACE)
                except asyncio.TimeoutError:
                    raise VKCaptchaSolverError(
                        op="solving: out of time (%g s)" % limit, cause=asyncio.TimeoutError()
                    ) from None
        except Exception as e:
            report.took = time.monotonic() - started
            report.failure = failure_kind(e)
            try:
                e.report = report  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover - exceptions that refuse attributes
                pass
            log.warning("VK %s not solved (%s): %s; %s", what, report.failure, e, report.describe())
            raise
        report.took = time.monotonic() - started
        log.info("VK %s solved: %s", what, report.describe())
        return result, report


_MAX_CACHED_SCRIPTS = 16


def _positive(value: Any, default: float, name: str) -> float:
    if value is None:
        return default
    v = float(value)
    if not v > 0:
        raise ValueError("%s must be positive" % name)
    return v


__all__ = [
    "CaptchaSolver",
    "VKCaptchaSolverError",
    "APIError",
    "HTTPError",
    "IApiOptions",
    "UnknownCaptchaTypeError",
    "Report",
    "failure_kind",
    "FAILURE_TIMEOUT",
    "FAILURE_BOT",
    "FAILURE_PAGE_CHANGED",
    "FAILURE_OTHER",
]
