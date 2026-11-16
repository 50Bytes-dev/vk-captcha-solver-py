"""The versions of the page's proof of work script (port of scripts_test.go)."""

import base64
import json
import math

import pytest

from conftest import TEST_CAPTCHA, new_bff_fake, page_running, probe_script, server_context
from vk_captcha_solver.jsontree import OBJECT, index_of, jget, jnum, parse_json
from vk_captcha_solver.profile import DEFAULT_PROFILE as P
from vk_captcha_solver.profile import RECORDED_SCRIPT_ID, new_run, vary_probe

# The order a page of 2026-11-07 gave its 23 fast probes.
DEVTOOLS_ORDER = [
    "ancestor_origins", "nav_tamper", "ua", "timezone_locale", "env_ua", "env_viewport", "origin_via_self", "plugins",
    "native_integrity", "ua_high_entropy", "match_media", "frame", "globals", "shadow_dom_check", "chrome_runtime",
    "css", "device_pixel_ratio", "referrer", "media_codecs", "cookie_test", "sandbox_behavior", "max_touch_points",
    "devtools",
]


def script(id_):
    return next(sc for sc in P.scripts if sc.id == id_)


def test_script_for_picks_the_pages_version():
    recorded, devtools = script(RECORDED_SCRIPT_ID), script("devtools-37")
    assert len(recorded.tel.keys) == 43 and len(devtools.tel.keys) == 37 and len(devtools.fast_probes) == 23
    recorded_order = recorded.tel.keys[: len(recorded.fast_probes)]
    for name, order, src, want in [
        ("the recorded script", recorded_order, probe_script(recorded, recorded_order), recorded),
        ("the devtools script", DEVTOOLS_ORDER, probe_script(devtools, DEVTOOLS_ORDER), devtools),
        ("no probes found, an order without devtools", ["globals", "ua"], "", recorded),
        ("no probes found, devtools ordered", ["globals", "devtools"], "", devtools),
    ]:
        assert P.script_for(order, src) is want, name

    for name, (order, src) in {
        "devtools ordered of the 43": (DEVTOOLS_ORDER, probe_script(recorded, recorded_order)),
        "an unknown probe ordered": (["globals", "new_probe"], ""),
        "the 37 with one more": (
            DEVTOOLS_ORDER,
            probe_script(devtools, DEVTOOLS_ORDER).replace("'globals':", "'new_probe':function(){},'globals':", 1),
        ),
        "the 37 without one": (
            DEVTOOLS_ORDER,
            probe_script(devtools, DEVTOOLS_ORDER).replace("await _0x90c457('wasm'", "await _0x90c457('Wasm'", 1),
        ),
        "the 43 without page_lifecycle": (
            recorded_order,
            probe_script(recorded, recorded_order).replace("'page_lifecycle_provenance'", "'x'", 1),
        ),
    }.items():
        with pytest.raises(ValueError) as ei:
            P.script_for(order, src)
        assert RECORDED_SCRIPT_ID + ": " in str(ei.value) and "devtools-37: " in str(ei.value), name


def test_devtools_script_is_the_recorded_browser():
    recorded, devtools = script(RECORDED_SCRIPT_ID), script("devtools-37")
    for name, item in zip(devtools.tel.keys, devtools.tel.items):
        if name == "devtools":
            continue
        rec = jget(recorded.tel, name)
        assert rec is not None, name
        assert jget(item, "duration_ms").text == jget(rec, "duration_ms").text, name
        res, rec_res = jget(item, "result"), jget(rec, "result")
        if res.kind != OBJECT:
            assert res.dumps() == rec_res.dumps(), name
            continue
        last = -1
        for key, v in zip(res.keys, res.items):
            k = index_of(rec_res.keys, key)
            assert k > last, "%s.%s is not in the recording, or out of its order" % (name, key)
            last = k
            assert v.dumps() == rec_res.items[k].dumps(), "%s.%s" % (name, key)

    dt = jget(devtools.tel, "devtools", "result")
    device = json.loads(P.device)
    geometry = jget(recorded.tel, "viewport_frame_geometry", "result")

    def n(v, key):
        return jnum(jget(v, key))[0]

    assert jget(dt, "open").text == "false" and n(dt, "delay_ms") <= 5
    assert n(dt, "innerWidth") == device["innerWidth"] and n(dt, "innerHeight") == device["innerHeight"]
    assert n(dt, "screenWidth") == device["screenWidth"] and n(dt, "screenHeight") == device["screenHeight"]
    assert n(dt, "outerWidth") == n(geometry, "outer_w") and n(dt, "outerHeight") == n(geometry, "outer_h")


def test_telemetry_for_devtools_script():
    devtools = script("devtools-37")
    r = new_run("https://id.vk.com/not_robot_captcha?domain=vk.com&session_token=NEW", 1800000000123, DEVTOOLS_ORDER, "")
    r.script = devtools
    r.timezone, r.tz_offset = "Europe/Moscow", -180
    tel = P.telemetry_for(r)
    got = parse_json(tel.json)
    assert got.keys == DEVTOOLS_ORDER + devtools.tel.keys[len(devtools.fast_probes) :]
    for gone in ("js_engine_oracle", "client_clock_snapshot", "page_lifecycle_provenance", "timer_resolution"):
        assert jget(got, gone) is None, gone
    assert "NEW" not in tel.json
    assert jget(got, "timezone_locale", "result", "timezone").text == "Europe/Moscow"
    assert jget(got, "devtools", "result", "open").text == "false"
    total = sum(jnum(jget(i, "duration_ms"))[0] for i in got.items)
    assert abs(tel.ready_ms - r.script_at - tel.duration_ms) <= 1
    assert total <= tel.duration_ms <= total * 1.2
    first = jnum(jget(got, "timing_baseline", "result", "res").items[0])[0]
    assert r.script_at <= first <= tel.ready_ms


def test_vary_devtools_probe():
    devtools = script("devtools-37")
    for i in range(200):
        tel = devtools.tel.clone()
        warm = i % 2 == 0
        for k, item in zip(tel.keys, tel.items):
            vary_probe(k, item, warm)
        dt = jget(tel, "devtools")
        d = jnum(jget(dt, "duration_ms"))[0]
        delay = jnum(jget(dt, "result", "delay_ms"))[0]
        assert jget(dt, "result", "open").text == "false" and delay <= 5 and d >= 0
        assert delay <= math.floor(d + 0.5)
        if warm:
            assert delay == 0 and d <= 0.2
    seen = set()
    for _ in range(20):
        tel = devtools.tel.clone()
        for k, item in zip(tel.keys, tel.items):
            vary_probe(k, item, False)
        seen.add(jget(tel, "speech_voices", "duration_ms").text)
    assert len(seen) >= 5, seen


async def test_solve_bff_devtools_page(make_solver):
    devtools = script("devtools-37")
    f = new_bff_fake("checkbox")
    f.set("id.vk.com/not_robot_captcha", page_running(probe_script(devtools, DEVTOOLS_ORDER)))
    await f.start(server_context())
    try:
        token, report = await make_solver(f).solve_report(TEST_CAPTCHA)
        assert token == "SUCCESS-TOKEN" and report.script == "devtools-37"
        raw = base64.b64decode(f.call("captchaNotRobot.check").f("hash")[3:]).decode()
        tel_text = raw[raw.index(',"telemetry":') + 13 : raw.rindex(',"tel_hash":')]
        tel = parse_json(tel_text)
        assert len(tel.keys) == 37 and tel.keys[: len(DEVTOOLS_ORDER)] == DEVTOOLS_ORDER
        assert jget(tel, "js_engine_oracle") is None
    finally:
        await f.close()
