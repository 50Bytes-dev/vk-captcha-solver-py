"""Errors of a solving and their kinds (port of ``errors.go``).

Error texts carry no URL, page body or proxy: VK's addresses hold session
tokens, and a proxy URL may hold a password. The URL and the body of a page
that lacked a value are attributes (``url``, ``body``), and the cause of a
failure is ``cause``.
"""

import asyncio
import re
from typing import Any, Iterator, Optional

# The kinds of a failed solving (failure_kind).
#: The connection failed or timed out, VK answered 5xx, or the solving ran
#: out of time.
FAILURE_TIMEOUT = "timeout"
#: VK's antibot answered BOT: the browser profile no longer passes.
FAILURE_BOT = "bot"
#: The captcha page or script lacks what the solver reads, or the captcha is
#: of an unknown kind: VK changed it.
FAILURE_PAGE_CHANGED = "page_changed"
#: Anything else.
FAILURE_OTHER = "failed"


class VKCaptchaSolverError(Exception):
    """A failure to solve a captcha.

    ``op`` is what failed (``"captcha page: powInput not found"``, an API
    method...), ``cause`` the underlying error if any. ``url`` and ``body`` are
    the page a value was missing from, after redirects, and that page as it
    came. ``transport`` marks a failure of the connection itself (no answer, a
    reset, a timeout); ``changed`` a page or script that lacks what the solver
    reads, or a captcha it does not know: VK changed it. ``report`` is how the
    solving went as far as it got (set by the solver).
    """

    def __init__(
        self,
        message: str = "",
        *,
        op: Optional[str] = None,
        cause: Optional[BaseException] = None,
        url: Optional[str] = None,
        body: Optional[str] = None,
        transport: bool = False,
        changed: bool = False,
    ) -> None:
        if not message and op:
            message = op
            if cause is not None:
                message += ": " + _safe_text(cause)
        super().__init__(message)
        self.op = op
        self.cause = cause
        self.url = url
        self.body = body
        self.transport = transport
        self.changed = changed
        self.report: Any = None


class UnknownCaptchaTypeError(VKCaptchaSolverError):
    """The captcha is neither a checkbox nor a slider (``page_changed``)."""

    def __init__(self, kind: str) -> None:
        super().__init__(
            'captcha type "%s": unknown captcha type' % kind,
            op='captcha type "%s"' % kind,
            changed=True,
        )
        self.kind = kind


class HTTPError(VKCaptchaSolverError):
    """A response with a status of 400 or more. ``url`` is the whole request
    URL and ``body`` the response body: attributes, not text."""

    def __init__(
        self,
        status: int,
        status_text: str,
        url: str,
        message: Optional[str] = None,
        *,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message or "HTTP Error %s: %s" % (status, status_text), url=url, body=body)
        self.status = status
        self.status_text = status_text


class APIError(VKCaptchaSolverError):
    """An error a captchaNotRobot.* method returned: an error object
    (``code``) or a response whose status is not OK (``status``).
    ``show_captcha_type`` is the captcha the response offers instead, if any:
    VK answers a checkbox it does not trust with BOT and "slider"."""

    def __init__(self, error: Any, method: str) -> None:
        if not isinstance(error, dict):
            error = {"error_msg": "unreadable error"}
        self.error = error
        self.method = method
        self.code: Optional[int] = error.get("error_code")
        self.status: Optional[str] = error.get("status") or None
        self.show_captcha_type: str = error.get("show_captcha_type") or ""
        self.error_msg: str = error.get("error_msg") or "Unknown error"

        if self.status:
            message = "[Status: %s] (%s) %s" % (self.status, method, self.error_msg)
            if self.show_captcha_type:
                message += "; show_captcha_type " + self.show_captcha_type
        else:
            message = "[Code: %s] (%s) %s" % (self.code if self.code is not None else 0, method, self.error_msg)
        super().__init__(message, op=method)


def page_error(op: str, url: Optional[str], body: Optional[str]) -> VKCaptchaSolverError:
    """A value missing from, or bad in, a page: VK changed it."""
    return VKCaptchaSolverError(op=op, url=url, body=body, changed=True)


def net_error(op: str, cause: BaseException) -> VKCaptchaSolverError:
    """A failure of the connection itself."""
    return VKCaptchaSolverError(op=op, cause=cause, transport=True)


_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\")>]*")
_USERINFO_RE = re.compile(r"[^\s/@'\"]+:[^\s/@'\"]*@")


def _safe_text(e: BaseException) -> str:
    """The text of a cause without URLs or credentials: aiohttp puts the
    request URL in some of its errors."""
    text = str(e) or type(e).__name__
    text = _URL_RE.sub("<url>", text)
    text = _USERINFO_RE.sub("<user>@", text)
    if text != type(e).__name__ and not isinstance(e, VKCaptchaSolverError):
        text = "%s: %s" % (type(e).__name__, text)
    return text


def _chain(err: Optional[BaseException]) -> Iterator[BaseException]:
    seen = set()
    while err is not None and id(err) not in seen:
        seen.add(id(err))
        yield err
        nxt = getattr(err, "cause", None) if isinstance(err, VKCaptchaSolverError) else None
        err = nxt if nxt is not None else err.__cause__


def _is_plain(e: BaseException) -> bool:
    return isinstance(e, VKCaptchaSolverError) and not isinstance(e, (HTTPError, APIError))


def failure_kind(err: Optional[BaseException]) -> str:
    """The kind of err, an error of a solving: ``bot`` and ``page_changed``
    mean the solver needs a new browser recording; ``timeout`` is VK or the
    network being slow; ``failed`` is anything else."""
    chain = list(_chain(err))
    api = next((e for e in chain if isinstance(e, APIError)), None)
    if api is not None and (api.status or "").upper() == "BOT":
        return FAILURE_BOT
    plain = next((e for e in chain if _is_plain(e)), None)
    if any(isinstance(e, UnknownCaptchaTypeError) for e in chain) or (plain is not None and plain.changed):
        return FAILURE_PAGE_CHANGED
    http = next((e for e in chain if isinstance(e, HTTPError)), None)
    if (
        (http is not None and http.status >= 500)
        or (plain is not None and plain.transport)
        or any(isinstance(e, (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError)) for e in chain)
    ):
        return FAILURE_TIMEOUT
    return FAILURE_OTHER
