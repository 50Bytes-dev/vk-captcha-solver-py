"""The browser profile of the VK ID captcha with proof of work v2 (port of
``profile.go``).

The BFF captcha page VK rolled out in the autumn of 2026 carries a browser's
telemetry in its proof of work. The profile is one real browser as the captcha
saw it: its telemetry, device, fingerprint and client hints were recorded
together from Chrome 156 on id.vk.com, and that run passed
captchaNotRobot.check (2026-11-03). They must agree with each other and with
the User-Agent, so the solver sends them as one.

VK serves more than one version of the page's proof of work script, and each
runs its own probes: the recorded run met one of 43 probes, while from
2026-11-06 pages asked for the devtools probe of a script of 37. The profile
holds the telemetry of each version it knows, the same browser's values
throughout, and a page gets the telemetry of the version it runs
(``Profile.script_for``).
"""

import base64
import hashlib
import os
import random
import re
from datetime import datetime, timezone as dt_timezone
from importlib import resources
from typing import Dict, List, Optional, Tuple

from .exceptions import VKCaptchaSolverError
from .jsontree import (
    ARRAY,
    OBJECT,
    STRING,
    JSONValue,
    ascii_json,
    canonical_js,
    go_round,
    index_of,
    iter_items,
    jget,
    jnum,
    js_number,
    js_string,
    jset_num,
    jset_str,
    literal,
    parse_json,
    round1,
)

PROFILE_FILE = "chrome156-macos.json"

#: The version of the proof of work script the profile was recorded on.
RECORDED_SCRIPT_ID = "recorded-43"

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_TZ_OFFSET = -180

# The values telemetry_for sets.
TELEMETRY_PATHS = [
    ("js_engine_oracle", "result", "stack_at"),
    ("client_clock_snapshot", "result", "now_ms"),
    ("client_clock_snapshot", "result", "perf_origin"),
    ("page_lifecycle_provenance", "result", "now_ms"),
    ("timezone_locale", "result", "timezone"),
    ("timezone_locale", "result", "tz_offset"),
    ("timing_baseline", "result", "res"),
]


class ProfileError(RuntimeError):
    """The browser profile shipped with the package is broken."""


class Script:
    """One version of the page's proof of work script: the probes it may
    reorder (in its own order) and the profile's telemetry of all its probes,
    as the script writes it."""

    def __init__(self, id: str, name: str, fast_probes: List[str], telemetry: str) -> None:
        self.id = id
        self.name = name
        self.fast_probes = fast_probes
        self.telemetry = telemetry  # the JSON text, as in the profile file
        self.tel: JSONValue = JSONValue(OBJECT)

    def must_read(self) -> None:
        """Read the telemetry: it must read back byte for byte, open with the
        fast probes, give every probe a duration, and hold every value
        telemetry_for sets of the probes it has."""
        try:
            tel = parse_json(self.telemetry)
            err = None
        except ValueError as e:
            tel, err = None, e
        if tel is None or tel.kind != OBJECT or tel.dumps() != self.telemetry:
            raise ProfileError(
                "browser profile %s: the telemetry does not read back byte for byte (%s)" % (self.id, err)
            )
        self.tel = tel
        for name in self.fast_probes:
            i = index_of(tel.keys, name)
            if i < 0 or i >= len(self.fast_probes):
                raise ProfileError("browser profile %s: the fast probes must open the telemetry: %s" % (self.id, name))
        for key, item in zip(tel.keys, tel.items):
            if not jnum(jget(item, "duration_ms"))[1]:
                raise ProfileError("browser profile %s: no duration_ms in %s" % (self.id, key))
        for path in TELEMETRY_PATHS:
            if jget(tel, path[0]) is not None and jget(tel, *path) is None:
                raise ProfileError("browser profile %s: the telemetry has no %s" % (self.id, ".".join(path)))

    def matches(self, order: List[str], found: List[str]) -> Optional[str]:
        """How the page's probes differ from the version's (None: they do not):
        order, the page's probe order, names a probe the version may not
        reorder, or found, the probes of the page's script (empty: not
        compared), are not the version's."""
        for name in order:
            if index_of(self.fast_probes, name) < 0:
                return "the page orders a probe it lacks: %s" % name
        extra = [n for n in found if jget(self.tel, n) is None]
        missing = [n for n in self.tel.keys if found and index_of(found, n) < 0]
        if extra or missing:
            return "the page's probes differ: new [%s], gone [%s]" % (" ".join(extra), " ".join(missing))
        return None

    def __repr__(self) -> str:  # pragma: no cover
        return "Script(%s)" % self.id


class RecordedRun:
    """The values of the recorded run that change from run to run."""

    def __init__(self, d: Dict) -> None:
        self.pow_input: str = d["powInput"]
        self.difficulty: int = d["difficulty"]
        self.hash: str = d["hash"]
        self.nonce: int = d["nonce"]
        self.duration_ms: int = d["durationMs"]
        self.tel_hash: str = d["telHash"]
        # The SHA-256 hex of the hash parameter Chrome sent: pow_v2 of this
        # run must make it byte for byte.
        self.hash_param_sha256: str = d["hashParamSHA256"]
        self.stack_at: str = d["stackAt"]
        self.now_ms: int = d["nowMs"]
        self.perf_origin: int = d["perfOrigin"]
        self.page_now_ms: int = d["pageNowMs"]
        self.timezone: str = d["timezone"]
        self.tz_offset: int = d["tzOffset"]


_PROBE_DEF_RE = re.compile(r"'([a-z][a-z0-9_]*)':\s*(?:async\s+)?function", re.ASCII)
_PROBE_CALL_RE = re.compile(r"\('([a-z][a-z0-9_]*)',\s*(?:async\s+)?function", re.ASCII)


class Profile:
    """A recorded browser."""

    def __init__(self, raw: str) -> None:
        try:
            tree = parse_json(raw)
        except ValueError as e:
            raise ProfileError("bad browser profile: %s" % e) from None

        def field(name: str, kind: int, of: Optional[JSONValue] = None) -> JSONValue:
            v = jget(of or tree, name)
            if v is None or v.kind != kind:
                raise ProfileError("bad browser profile: no %s" % name)
            return v

        def strings(v: JSONValue) -> List[str]:
            if any(i.kind != STRING for i in v.items):
                raise ProfileError("bad browser profile: a list of names holds a non-string")
            return [i.text for i in v.items]

        self.name = field("name", STRING).text
        self.user_agent = field("userAgent", STRING).text
        hints = field("clientHints", OBJECT)
        self.client_hints: Dict[str, str] = {k: v.text for k, v in zip(hints.keys, hints.items)}
        # device is the device parameter of componentDone, as the browser sent it.
        self.device = field("device", STRING).text
        self.browser_fp = field("browserFP", STRING).text
        # fast_probes are the probes the page may reorder, in the order of the
        # page's script when the page names none.
        self.fast_probes = strings(field("fastProbes", ARRAY))
        from .jsontree import to_python

        self.recorded = RecordedRun(to_python(field("recorded", OBJECT)))
        tel_node = field("telemetry", OBJECT)
        # The telemetry as the profile file writes it (Go's json.RawMessage).
        self.telemetry = raw[tel_node.span[0] : tel_node.span[1]]

        recorded = Script(RECORDED_SCRIPT_ID, self.name, self.fast_probes, self.telemetry)
        self.scripts: List[Script] = [recorded]
        scripts = jget(tree, "scripts")
        for sc in iter_items(scripts):
            t = field("telemetry", OBJECT, sc)
            self.scripts.append(
                Script(
                    field("id", STRING, sc).text,
                    field("name", STRING, sc).text,
                    strings(field("fastProbes", ARRAY, sc)),
                    raw[t.span[0] : t.span[1]],
                )
            )
        for sc in self.scripts:
            sc.must_read()
        self.tel = recorded.tel
        for path in TELEMETRY_PATHS:
            if jget(self.tel, *path) is None:
                raise ProfileError("browser profile: the telemetry has no %s" % ".".join(path))

        # The probes run one after another, and the proof of work reports when
        # the last is done: its duration_ms is their time and the gaps between.
        total = 0.0
        for item in self.tel.items:
            total += jnum(jget(item, "duration_ms"))[0]
        self.gap = (float(self.recorded.duration_ms) - total) / float(len(self.tel.items))
        self.script_at = float(self.recorded.page_now_ms) - self._elapsed_before(
            self.tel, "page_lifecycle_provenance", self.gap
        )
        self.timing_baseline_at = self.script_at + self._elapsed_before(self.tel, "timing_baseline", self.gap)

        # stack_at is "    at <page URL>:<line>:<column>".
        at = self.recorded.stack_at
        i = at.rfind(":")
        j = at[: max(i, 0)].rfind(":")
        if i < 0 or j < 0:
            raise ProfileError("browser profile: stackAt has no line and column")
        self.stack_pos = at[j + 1 :]

    @staticmethod
    def _elapsed_before(tel: JSONValue, name: str, gap: float) -> float:
        """The time from the script's start to probe name."""
        elapsed = 0.0
        for k, item in zip(tel.keys, tel.items):
            if k == name:
                return elapsed
            elapsed += jnum(jget(item, "duration_ms"))[0] + gap
        raise ProfileError("browser profile: the telemetry has no %s" % name)

    def script_for(self, order: List[str], src: str) -> Script:
        """The version of the proof of work script the page runs: the one whose
        probes are the page's. order is the page's probe order and src its
        proof of work script; a script the probe patterns find nothing in is
        matched by order alone, the first version that has every probe of it.
        A page no version matches is an error: the telemetry would show a
        browser that cannot be (VK changed the script; the profile needs a new
        version recorded)."""
        found: List[str] = []
        for rx in (_PROBE_DEF_RE, _PROBE_CALL_RE):
            for m in rx.finditer(src):
                if index_of(found, m.group(1)) < 0:
                    found.append(m.group(1))
        why = []
        for sc in self.scripts:
            err = sc.matches(order, found)
            if err is None:
                return sc
            why.append(sc.id + ": " + err)
        raise ValueError("the page's probes match no version of the browser profile: " + "; ".join(why))

    def telemetry_for(self, r: "Run") -> "Telemetry":
        """The profile's telemetry as the page's script writes it in run r: the
        probes in the page's order, with r's page address, clock and time zone,
        and, if r.vary, new values where a browser measures anew on every load.
        The clock follows the probes' durations: the script runs them one after
        another from its start."""
        sc = r.script or self.scripts[0]
        t = sc.tel.clone()
        t.reorder(probe_order(sc.fast_probes, r.order))
        script_at, gap = self.script_at, self.gap
        if r.vary:
            for name, item in zip(t.keys, t.items):
                vary_probe(name, item, r.warm)
            script_at, gap = r.script_at, self.gap * r.gap_scale

        elapsed = 0.0
        starts: Dict[str, float] = {}
        for name, item in zip(t.keys, t.items):
            starts[name] = script_at + elapsed
            elapsed += jnum(jget(item, "duration_ms"))[0] + gap

        page_now = go_round(starts.get("page_lifecycle_provenance", 0.0))
        nav_ms = float(r.nav_start_ms)
        # A script without a probe leaves its values out.
        jset_num(jget(t, "page_lifecycle_provenance", "result"), "now_ms", page_now)
        clock = jget(t, "client_clock_snapshot", "result")
        jset_num(clock, "now_ms", nav_ms + page_now)
        jset_num(clock, "perf_origin", nav_ms)
        # The readings are the recorded run's, taken at timing_baseline_at.
        res = jget(t, "timing_baseline", "result", "res")
        shift = round1(starts.get("timing_baseline", 0.0) - self.timing_baseline_at)
        if res is not None and (shift != 0 or r.vary):
            shift_readings(res, shift, r.vary)
        stack_pos = r.stack_pos or self.stack_pos
        jset_str(jget(t, "js_engine_oracle", "result"), "stack_at", "    at " + r.page_url + ":" + stack_pos)
        tz = jget(t, "timezone_locale", "result")
        jset_str(tz, "timezone", r.timezone)
        jset_num(tz, "tz_offset", float(r.tz_offset))

        return Telemetry(t.dumps(), int(go_round(elapsed)), script_at + elapsed)


class Telemetry:
    """One run's telemetry and what the page's script makes of it."""

    def __init__(self, json: str, duration_ms: int, ready_ms: float) -> None:
        self.json = json
        # The proof of work's duration_ms: the script reports only when the
        # last probe is done.
        self.duration_ms = duration_ms
        # The page's age (ms) when the telemetry was done: the widget sends no
        # check before it.
        self.ready_ms = ready_ms


class Run:
    """What changes in the telemetry from one page load to the next.

    vary makes what a real browser measures anew on every load differ from the
    recording (vary_probe) and starts the script at script_at with the gaps
    scaled by gap_scale; False keeps the recorded run's own values (tests).
    warm is a load of a browser that has run the page before."""

    def __init__(
        self,
        page_url: str,
        nav_start_ms: int,
        order: List[str],
        stack_pos: str = "",
        timezone: str = DEFAULT_TIMEZONE,
        tz_offset: int = DEFAULT_TZ_OFFSET,
        script: Optional[Script] = None,
        vary: bool = False,
        warm: bool = False,
        script_at: float = 0.0,
        gap_scale: float = 1.0,
    ) -> None:
        self.page_url = page_url
        self.nav_start_ms = nav_start_ms  # performance.timeOrigin, Unix ms
        self.order = order
        self.stack_pos = stack_pos
        self.timezone = timezone
        self.tz_offset = tz_offset  # minutes, as Date.getTimezoneOffset: UTC - local
        self.script = script
        self.vary = vary
        self.warm = warm
        self.script_at = script_at
        self.gap_scale = gap_scale


def new_run(
    page_url: str,
    nav_start_ms: int,
    order: List[str],
    stack_pos: str,
    tz_name: Optional[str] = None,
    loaded_ms: float = 0.0,
) -> Run:
    """A load of the captcha page at page_url, asked for at nav_start_ms (Unix
    ms), in the time zone resolve_timezone(tz_name) gives. loaded_ms is how
    long the page took to come: the script starts no sooner than 30-90 ms
    after it (Python's own rule: this library runs through slow proxies)."""
    name, offset = resolve_timezone(tz_name)
    # The solver is one browser that meets the captcha again and again: most
    # of its loads are warm.
    warm = random.random() < 0.75
    r = Run(
        page_url,
        nav_start_ms,
        order,
        stack_pos,
        timezone=name,
        tz_offset=offset,
        vary=True,
        warm=warm,
        gap_scale=rand_range(0.7, 1.4),
    )
    # The recorded script started 407 ms into the page, after the page's other
    # scripts came; a warm browser has them cached.
    r.script_at = rand_range(385, 445)
    if r.warm:
        r.script_at = rand_range(300, 450)
    if loaded_ms > 0:
        r.script_at = max(r.script_at, loaded_ms + rand_range(30, 90))
    return r


def probe_order(fast: List[str], page: List[str]) -> List[str]:
    """The order the page's script runs the fast probes in: those the page
    names first, in its order, then the rest in the script's own (fast)."""
    out: List[str] = []
    for name in page:
        if index_of(fast, name) >= 0 and index_of(out, name) < 0:
            out.append(name)
    for name in fast:
        if index_of(out, name) < 0:
            out.append(name)
    return out


def vary_probe(name: str, p: JSONValue, warm: bool) -> None:
    """Give probe p of the telemetry new values where Chrome measures anew on
    every load, around the recorded ones: its duration and the timings, frame
    and loop counts some probes report. Fingerprints stay as recorded.

    The recording was the first load of a fresh browser: listing media devices
    took 756 ms, the speech voices came after 175 ms, wasm compiled in 20 ms.
    Loads after the first take what warm gives them instead."""
    res = jget(p, "result")
    d = jnum(jget(p, "duration_ms"))[0]
    if name == "devtools":
        # The time of new Function('debugger;')() with DevTools closed: under
        # 0.1 ms on a warm load, about 1 ms on the first. delay_ms is that time
        # rounded; open stays false (over 5 ms).
        if warm:
            jset_num(res, "delay_ms", 0)
            jset_num(p, "duration_ms", pick([0, 0.1, 0.1, 0.1, 0.2]))
            return
        d2 = round1(rand_range(0.7, 1.3))
        jset_num(res, "delay_ms", go_round(d2 - 0.1))
        jset_num(p, "duration_ms", d2)
        return
    if warm:
        if name == "media_devices":
            jset_num(p, "duration_ms", round1(rand_range(0.5, 2.4)))
            return
        if name == "speech_voices":
            w = float(rand_int(0, 2))
            jset_num(res, "waited_ms", w)
            jset_num(p, "duration_ms", round1(max(0.5, w + rand_range(-0.2, 0.9))))
            return
        if name == "wasm":
            c, e = round1(rand_range(0.1, 0.3)), round1(rand_range(0.1, 0.4))
            jset_num(res, "compileMs", c)
            jset_num(res, "execMs", e)
            jset_num(p, "duration_ms", round1(c + e + rand_range(0, 0.2)))
            return
        if name == "font_enumeration":
            jset_num(p, "duration_ms", round1(rand_range(44.5, 48.5)))
            return
        if name == "canvas_fingerprint":
            jset_num(p, "duration_ms", round1(rand_range(1.5, 3.9)))
            return
        if name == "gpu_render_timing":
            window = jnum(jget(res, "window_ms"))[0]
            jset_num(res, "frames", float(rand_int(160, 260)))
            jset_num(res, "window_ms", round1(window + float(rand_int(0, 9)) / 10))
            jset_num(res, "first_frame_ms", round1(rand_range(0.5, 0.9)))
            jset_num(p, "duration_ms", round1(rand_range(155.5, 161.5)))
            return

    if name == "wasm":
        c = jnum(jget(res, "compileMs"))[0]
        e = jnum(jget(res, "execMs"))[0]
        c2, e2 = round1(c * rand_range(0.8, 1.25)), round1(e * rand_range(0.8, 1.25))
        jset_num(res, "compileMs", c2)
        jset_num(res, "execMs", e2)
        jset_num(p, "duration_ms", round1(c2 + e2 + d - c - e))
        return
    if name == "visibility_raf":
        dm = jnum(jget(res, "durationMs"))[0]
        fps = jnum(jget(res, "fps"))[0]
        dm2 = round1(dm + rand_range(-4, 9))
        jset_num(res, "durationMs", dm2)
        jset_num(res, "fps", fps + pick([-1, 0, 0, 1]))
        jset_num(p, "duration_ms", round1(dm2 + d - dm))
        return
    if name == "speech_voices":
        if jget(res, "waited_ms") is not None:
            w = jnum(jget(res, "waited_ms"))[0]
            w2 = go_round(w * rand_range(0.8, 1.25))
            jset_num(res, "waited_ms", w2)
            jset_num(p, "duration_ms", round1(w2 + d - w))
            return
        # A script that does not report the wait: the duration alone.
    elif name == "timer_resolution":
        z = jnum(jget(res, "perf_zero"))[0]
        reads = jnum(jget(res, "perf_reads"))[0]
        z2 = go_round(z * rand_range(0.8, 1.1))
        jset_num(res, "perf_zero", z2)
        jset_num(res, "perf_reads", z2 + reads - z)
        jset_num(p, "duration_ms", round1(d + pick([-0.1, 0, 0.1])))
        return
    elif name == "reflow_timing":
        for tm in iter_items(jget(res, "timings")):
            ms, ok = jnum(jget(tm, "ms"))
            if ok and ms <= 0.1:
                jset_num(tm, "ms", pick([0, 0.1]))
    elif name == "timing_baseline":
        for item in iter_items(jget(res, "cpu")):
            x, ok = jnum(item)
            if ok:
                item.assign(literal(js_number(max(0.0, round1(x + pick([-0.1, 0, 0.1]))))))
    elif name == "gpu_render_timing":
        frames = jnum(jget(res, "frames"))[0]
        window = jnum(jget(res, "window_ms"))[0]
        jset_num(res, "frames", go_round(frames * rand_range(0.75, 1.1)))
        jset_num(res, "window_ms", round1(window + float(rand_int(0, 9)) / 10))
        jset_num(res, "first_frame_ms", round1(rand_range(0.4, 0.9)))
    jset_num(p, "duration_ms", vary_duration(d))


def vary_duration(d: float) -> float:
    """A probe's duration_ms on another load: within a tenth of a millisecond
    for the quick ones, within -15..+20 % for the rest."""
    if d < 1:
        return max(0.0, round1(d + pick([-0.1, 0, 0, 0.1])))
    return round1(d * rand_range(0.85, 1.2))


def shift_readings(res: JSONValue, shift: float, vary: bool) -> None:
    """Move performance.now() readings by shift ms, and, if vary, each of them
    after the first by up to 0.1 ms more."""
    for i, item in enumerate(res.items):
        x, ok = jnum(item)
        if not ok:
            continue
        x += shift
        if vary and i > 0:
            x += pick([-0.1, 0, 0.1])
        item.assign(literal(js_number(x)))


def rand_range(lo: float, hi: float) -> float:
    """A random number in [lo, hi)."""
    return lo + random.random() * (hi - lo)


def rand_int(lo: int, hi: int) -> int:
    """A random integer in [lo, hi], both ends included."""
    return random.randint(lo, hi)


def pick(xs):
    return random.choice(xs)


# -- the v2 hash ---------------------------------------------------------------


def pow_v2(hash_hex: str, nonce: int, duration_ms: int, telemetry: str) -> str:
    """The hash parameter of captchaNotRobot.check on a page with proof of
    work v2: "v2." and the base64 of the JSON the page's script makes of the
    proof of work and the telemetry, with the SHA-256 of the telemetry's
    canonical JSON as tel_hash. The JSON is ASCII, as the script writes it."""
    tel_hash = telemetry_hash(telemetry)
    doc = (
        '{"hash":'
        + js_string(hash_hex)
        + ',"nonce":'
        + str(int(nonce))
        + ',"error":"","duration_ms":'
        + str(int(duration_ms))
        + ',"telemetry":'
        + telemetry
        + ',"tel_hash":'
        + js_string(tel_hash)
        + "}"
    )
    return "v2." + base64.b64encode(ascii_json(doc).encode("ascii")).decode("ascii")


def telemetry_hash(telemetry: str) -> str:
    """The SHA-256 hex of the canonical JSON of telemetry."""
    try:
        v = parse_json(telemetry)
    except ValueError as e:
        raise VKCaptchaSolverError(op="telemetry: bad JSON", cause=e) from None
    return hashlib.sha256(canonical_js(v).encode("utf-8", "surrogatepass")).hexdigest()


# -- the browser around the profile ---------------------------------------------

_AD_FP_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def ad_fingerprint() -> str:
    """A made-up adFp: the id the ad.mail.ru sync loader gives the browser
    (window.rb_sync.id), 21 letters and digits."""
    return "".join(random.choice(_AD_FP_ALPHABET) for _ in range(21))


_PROCESS_AD_FP: Optional[str] = None


def process_ad_fingerprint() -> str:
    """The adFp of this process: a browser keeps its window.rb_sync.id, as it
    keeps its browser_fp, and the profile is one browser."""
    global _PROCESS_AD_FP
    if _PROCESS_AD_FP is None:
        _PROCESS_AD_FP = ad_fingerprint()
    return _PROCESS_AD_FP


def timezone_name(option: Optional[str] = None) -> str:
    """The IANA time zone the browser claims: the solver option, then
    VK_CAPTCHA_TIMEZONE, then TZ when it names a zone (holds a "/"), else
    Europe/Moscow. /etc/localtime is not read: the servers this runs on are
    UTC containers, and a ru-RU Chrome in UTC is rarer than one in Moscow."""
    for candidate in (option, os.environ.get("VK_CAPTCHA_TIMEZONE")):
        if candidate and candidate.strip():
            return candidate.strip().lstrip(":")
    tz = os.environ.get("TZ") or ""
    if "/" in tz:
        return tz.strip().lstrip(":")
    return DEFAULT_TIMEZONE


def resolve_timezone(option: Optional[str] = None, at: Optional[datetime] = None) -> Tuple[str, int]:
    """The time zone name and its Date.getTimezoneOffset() (minutes, UTC minus
    local) at the time at (now): one consistent pair. A zone that cannot be
    loaded falls back to Europe/Moscow, -180."""
    name = timezone_name(option)
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(name)
        offset = (at or datetime.now(dt_timezone.utc)).astimezone(zone).utcoffset()
        if offset is None:
            raise ValueError(name)
        return name, -int(go_round(offset.total_seconds() / 60))
    except Exception:
        return DEFAULT_TIMEZONE, DEFAULT_TZ_OFFSET


def _load_default_profile() -> Profile:
    raw = resources.files(__package__).joinpath("profiles").joinpath(PROFILE_FILE).read_bytes()
    return Profile(raw.decode("utf-8"))


#: The profile the solver uses.
DEFAULT_PROFILE = _load_default_profile()
