"""How a captcha is solved, page by page (port of ``solver.go``)."""

import asyncio
import base64
import hashlib
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .api import InitialData, InitialParams, Session, form_json, where
from .checkbox_solver import checkbox_sensors, drag_trace, slider_fraction
from .exceptions import APIError, UnknownCaptchaTypeError, VKCaptchaSolverError
from .profile import DEFAULT_PROFILE, new_run, pow_v2
from .slider_solver import solve_slider
from .utils import (
    generate_fake_desktop_environment,
    generate_fake_fingerprint_from_environment,
    get_random_float,
    get_random_int,
)

# base64("{}"), the answer of a checkbox captcha.
EMPTY_ANSWER = "e30="

# How the solver times a BFF page's check (seconds). The widget sends no check
# before the page's telemetry is done, and a person clicks the checkbox some
# time after the page shows, or drags the slider some time after its picture
# comes. CHECK_RESERVE is the time the check and endSession need after the
# wait.
MIN_CLICK = 0.150
CLICK_FROM, CLICK_TO = 0.400, 1.400
MIN_DRAG = 0.700
DRAG_FROM, DRAG_TO = 1.000, 1.700
CHECK_RESERVE = 0.700


async def solve(ss: Session, redirect_uri: str) -> str:
    """One captcha from its page to the success_token."""
    initial = await ss.initial_params(redirect_uri)
    if initial.v2:
        return await solve_v2(ss, initial)
    ss.report.bff = False
    debug_info = await ss.debug_info(initial.script_url)

    kind = initial.data.show_captcha_type
    ss.report.kind = kind
    if kind not in ("checkbox", "slider"):
        raise UnknownCaptchaTypeError(kind)

    sensors_delay, sensors = await ss.settings(initial.data)
    hash_hex, _ = await proof_of_work_from(ss, initial.pow_input, initial.difficulty, 1)

    device = generate_fake_desktop_environment()
    params = check_params(initial.data, hash_hex, debug_info, device, ss.s._timezone_pair())

    if kind == "checkbox":
        await ss.component_done(initial.data, device)
        params.update(checkbox_sensors(sensors, int(sensors_delay)))
    else:
        content = await ss.content(initial.data)
        await ss.component_done(initial.data, device)
        swaps = await _solve_slider(content)
        params["answer"] = slider_answer(swaps)

    token = await ss.check(sorted(params.items()))
    await ss.end_session(initial.data)
    return token


async def solve_v2(ss: Session, initial: InitialParams) -> str:
    """A captcha of a BFF page, run as the widget runs it in Chrome: settings,
    componentDone (after getContent for a slider), check and endSession. The
    proof of work carries the profile's telemetry for this load of the page
    (telemetry_for), and the check goes when a person would send it once the
    telemetry is done. A checkbox VK answers BOT and offers a slider for goes
    on to the slider, as the widget does (slider_after_bot)."""
    ss.report.bff = True
    kind = initial.data.show_captcha_type
    ss.report.kind = kind
    if kind not in ("checkbox", "slider"):
        raise UnknownCaptchaTypeError(kind)
    sensors_delay, _ = await ss.settings(initial.data)
    pow_hex, nonce = await proof_of_work_from(ss, initial.pow_input, initial.difficulty, 0)
    r = new_run(
        initial.page_url,
        initial.nav_start_ms,
        initial.probe_order,
        initial.stack_pos,
        tz_name=ss.s._timezone,
        loaded_ms=initial.loaded * 1000,
    )
    r.script = initial.script
    ss.report.warm = r.warm
    if r.script is not None:
        ss.report.script = r.script.id
    tel = DEFAULT_PROFILE.telemetry_for(r)
    hash_param = pow_v2(pow_hex, nonce, tel.duration_ms, tel.json)
    ready = initial.nav_start + tel.ready_ms / 1000

    answer: str = EMPTY_ANSWER
    cursor: List[Dict[str, int]] = []
    if kind == "checkbox":
        await ss.component_done_v2(initial.data, "")
        at = check_time(ss.deadline, "checkbox", ready + MIN_CLICK, ready + rand_duration(CLICK_FROM, CLICK_TO))
        await ss.s._wait(at)
        ss.report.check_at = at - initial.nav_start
    else:
        content = await ss.content(initial.data)
        shown = time.monotonic()
        await ss.component_done_v2(initial.data, "")
        swaps = await _solve_slider(content)
        answer = slider_answer(swaps)
        start = max(ready, shown)
        at = check_time(ss.deadline, "slider", start + MIN_DRAG, start + rand_duration(DRAG_FROM, DRAG_TO))
        cursor = drag_trace(sensors_delay, at - shown, slider_fraction(swaps, content.get("steps")))
        await ss.s._wait(at)
        ss.report.check_at = at - initial.nav_start

    try:
        token = await check_v2(ss, initial.data, initial.debug_info, hash_param, answer, cursor)
    except APIError as e:
        if kind == "checkbox" and (e.status or "").upper() == "BOT" and e.show_captcha_type == "slider":
            token = await slider_after_bot(ss, initial, sensors_delay, hash_param)
        elif kind == "slider":
            raise slider_verdict(e) from None
        else:
            raise
    await ss.end_session(initial.data)
    return token


async def slider_after_bot(ss: Session, initial: InitialParams, sensors_delay: float, hash_param: str) -> str:
    """Go on with the slider VK offered for a checkbox it answered BOT, as the
    widget does: getContent with the slider's settings, componentDone (the
    fingerprint is ready by now), and a check with the slider's answer and the
    cursor of the move to it and the drag, no sooner than a person could drag
    it. The proof of work is the page's, as before."""
    bot_at = time.monotonic()
    ss.report.slider_after_bot = True
    data = initial.data.copy()
    data.show_captcha_type = "slider"
    content = await ss.content(data)
    shown = time.monotonic()
    await ss.component_done_v2(data, DEFAULT_PROFILE.browser_fp)
    swaps = await _solve_slider(content)
    at = check_time(
        ss.deadline,
        "slider VK offered after BOT",
        shown + MIN_DRAG,
        shown + rand_duration(DRAG_FROM, DRAG_TO),
    )
    cursor = drag_trace(sensors_delay, at - bot_at, slider_fraction(swaps, content.get("steps")))
    await ss.s._wait(at)
    ss.report.check_at = at - initial.nav_start
    try:
        return await check_v2(ss, data, initial.debug_info, hash_param, slider_answer(swaps), cursor)
    except APIError as e:
        raise slider_verdict(e) from None


def slider_verdict(err: APIError) -> VKCaptchaSolverError:
    """The error of a slider's check. VK answers a slider answer it did not
    take with BOT and the slider offered again: the widget counts an error and
    lets the person try once more. That says the answer was wrong, not that the
    browser is not one: a failure that pauses nothing (failed), not bot. The
    solver gives one answer per captcha."""
    if (err.status or "").upper() == "BOT" and err.show_captcha_type == "slider":
        return VKCaptchaSolverError(
            op="captchaNotRobot.check: VK did not take the slider's answer and offered the slider again"
        )
    return err


async def check_v2(
    ss: Session, data: InitialData, debug_info: str, hash_param: str, answer: str, cursor: List[Dict[str, int]]
) -> str:
    """A BFF page's check as the widget writes it: the five sensors,
    browser_fp, the proof of work, the answer and debug_info."""
    return await ss.check(
        data.session_fields()
        + [
            ("accelerometer", []),
            ("gyroscope", []),
            ("motion", []),
            ("cursor", cursor),
            ("taps", []),
            ("browser_fp", DEFAULT_PROFILE.browser_fp),
            ("hash", hash_param),
            ("answer", answer),
            ("debug_info", debug_info),
        ]
    )


def check_time(deadline: Optional[float], what: str, not_before: float, at: float) -> float:
    """When to send a check a person would send at at, and no sooner than
    not_before (monotonic s): at, or as late as the deadline allows with
    CHECK_RESERVE left. An error when the deadline comes before not_before
    does: a check sent sooner would show what no browser can do."""
    if at < not_before:
        at = not_before
    if deadline is not None:
        latest = deadline - CHECK_RESERVE
        if latest < not_before:
            raise VKCaptchaSolverError(
                op="check of the " + what + ": too little time left to send it when a person could",
                cause=asyncio.TimeoutError(),
            )
        if at > latest:
            at = latest
    return at


async def wait_until(until: float) -> None:
    """Wait until the monotonic time until."""
    d = until - time.monotonic()
    if d > 0:
        await asyncio.sleep(d)


def rand_duration(lo: float, hi: float) -> float:
    """A random duration in [lo, hi)."""
    return lo + random.random() * (hi - lo)


def check_params(
    data: InitialData, hash_hex: str, debug_info: str, device: Dict[str, Any], tz: Tuple[str, int]
) -> Dict[str, Any]:
    """The old page's captchaNotRobot.check parameters before the answer: empty
    sensors, the connection the fake browser reports, the proof of work and the
    fingerprint."""
    rtt = get_random_int(40, 60)
    downlink = get_random_float(3, 10)
    rtts: List[int] = []
    if random.random() < 0.5:
        rtts = [rtt] * get_random_int(5, 10)
    return {
        "domain": data.domain,
        "session_token": data.session_token,
        "hash": hash_hex,
        "answer": EMPTY_ANSWER,
        "accelerometer": [],
        "cursor": [],
        "gyroscope": [],
        "motion": [],
        "taps": [],
        "connectionRtt": rtts,
        "connectionDownlink": [downlink] * get_random_int(5, 10),
        "debug_info": debug_info,
        "browser_fp": generate_fake_fingerprint_from_environment(device, tz),
    }


def slider_answer(swaps: Optional[List[int]]) -> str:
    """The slider answer: base64 of {"value": swaps}, compact JSON as VK's own
    script writes it."""
    return base64.b64encode(form_json({"value": list(swaps or [])}).encode("utf-8")).decode("ascii")


async def _solve_slider(content: Dict[str, Any]) -> List[int]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, solve_slider, content)


async def proof_of_work_from(ss: Optional[Session], input_str: str, difficulty: int, start: int) -> Tuple[str, int]:
    """The proof of work in a worker thread: it stops when the solving's time
    is out or the solving is cancelled."""
    stop = threading.Event()
    deadline = ss.deadline if ss is not None else None
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, pow_search, input_str, difficulty, start, deadline, stop)
    finally:
        stop.set()


def pow_search(
    input_str: str,
    difficulty: int,
    start: int,
    deadline: Optional[float] = None,
    stop: Optional[threading.Event] = None,
) -> Tuple[str, int]:
    """The first nonce from start for which the SHA-256 hex of input+nonce
    starts with difficulty zeros, and that hex. The old page counts from 1,
    the v2 script from 0."""
    if difficulty < 0 or difficulty > 64:
        raise VKCaptchaSolverError(
            op="proof of work: difficulty %d is out of range" % difficulty,
            changed=True,
        )
    prefix = "0" * difficulty
    base = hashlib.sha256(input_str.encode("utf-8"))
    nonce = start
    while True:
        if nonce % 4096 == 0:
            if (deadline is not None and time.monotonic() > deadline) or (stop is not None and stop.is_set()):
                raise VKCaptchaSolverError(op="proof of work", cause=asyncio.TimeoutError())
        h = base.copy()
        h.update(str(nonce).encode("ascii"))
        digest = h.hexdigest()
        if digest.startswith(prefix):
            return digest, nonce
        nonce += 1


def proof_of_work(input_str: str, difficulty: int) -> str:
    """The old captcha page's proof of work: the first nonce counting from 1."""
    return pow_search(input_str, difficulty, 1)[0]


__all__ = ["solve", "solve_v2", "check_time", "wait_until", "pow_search", "proof_of_work", "slider_answer", "where"]
