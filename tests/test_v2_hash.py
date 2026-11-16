"""The v2 hash, the telemetry model and the JSON tree (port of v2_test.go,
first half)."""

import hashlib
import math
import random

import pytest

from vk_captcha_solver.api import oracle_position
from vk_captcha_solver.jsontree import go_round, jget, jnum, js_number, parse_json, round1
from vk_captcha_solver.profile import DEFAULT_PROFILE as P
from vk_captcha_solver.profile import Run, new_run, pow_v2, telemetry_hash, vary_probe
from vk_captcha_solver.solver import pow_search


def test_pow_v2_rebuilds_chromes_hash():
    """The recorded run rebuilds the hash parameter Chrome sent, byte for byte:
    the proof of work counted from 0, the telemetry's canonical SHA-256 and the
    ASCII base64 envelope."""
    rec = P.recorded
    h, nonce = pow_search(rec.pow_input, rec.difficulty, 0)
    assert (h, nonce) == (rec.hash, rec.nonce)
    assert telemetry_hash(P.telemetry) == rec.tel_hash
    param = pow_v2(rec.hash, rec.nonce, rec.duration_ms, P.telemetry)
    assert hashlib.sha256(param.encode()).hexdigest() == rec.hash_param_sha256


def recorded_run_of(p) -> Run:
    """The profile's own run: its page, clock, probe order and time zone,
    nothing varied."""
    at = p.recorded.stack_at[: -len(":" + p.stack_pos)]
    return Run(
        at[len("    at ") :],
        p.recorded.perf_origin,
        p.tel.keys[: len(p.fast_probes)],
        timezone=p.recorded.timezone,
        tz_offset=p.recorded.tz_offset,
    )


def test_telemetry_for_recorded_run_is_chromes():
    """The model of the page's script gives back the recorded telemetry for the
    recorded run, byte for byte, with its duration_ms and a page age that fits
    its clock."""
    rec = P.recorded
    tel = P.telemetry_for(recorded_run_of(P))
    assert tel.json == P.telemetry
    assert tel.duration_ms == rec.duration_ms
    assert rec.page_now_ms <= int(tel.ready_ms) <= rec.page_now_ms + 10


def test_telemetry_for_new_run():
    """A new run runs the probes in the page's order, puts its page address,
    clock, oracle position and time zone in, keeps the clock in step with the
    probes' durations, and changes what a browser measures anew on each load,
    but no fingerprint."""
    page = "https://id.vk.com/not_robot_captcha?domain=vk.com&session_token=NEW-TOKEN&variant=popup&blank=1"
    nav = 1800000000123
    r = new_run(page, nav, ["frame", "ua", "nonsense", "globals"], "120:5")
    r.timezone, r.tz_offset = "Europe/Moscow", -180

    tel = P.telemetry_for(r)
    got = parse_json(tel.json)
    want = ["frame", "ua", "globals"]
    want += [n for n in P.fast_probes if n not in want]
    want += P.tel.keys[len(P.fast_probes) :]
    assert got.keys == want

    def s(*path):
        return jget(got, *path).text

    def n(*path):
        return jnum(jget(got, *path))[0]

    assert s("js_engine_oracle", "result", "stack_at") == "    at " + page + ":120:5"
    assert s("timezone_locale", "result", "timezone") == "Europe/Moscow"
    assert n("timezone_locale", "result", "tz_offset") == -180
    page_now = n("page_lifecycle_provenance", "result", "now_ms")
    assert n("client_clock_snapshot", "result", "perf_origin") == nav
    assert n("client_clock_snapshot", "result", "now_ms") == nav + page_now
    # The script starts r.script_at into the page and reports when the last
    # probe is done; page_lifecycle_provenance is the third from the end.
    assert abs(tel.ready_ms - r.script_at - tel.duration_ms) <= 1
    assert tel.ready_ms - 20 <= page_now <= tel.ready_ms
    res = jget(got, "timing_baseline", "result", "res")
    for a, b in zip(res.items, res.items[1:]):
        step = jnum(b)[0] - jnum(a)[0]
        assert 3 <= step <= 6, "timing_baseline.res steps %s -> %s" % (a.text, b.text)

    # Fingerprints stay as recorded.
    for probe in (
        "webgl", "canvas_fingerprint", "audio_sig", "ua", "pristine_realm", "font_enumeration",
        "subpixel_font", "dom_chain", "plugins", "css", "media_codecs",
    ):
        assert jget(got, probe, "result").dumps() == jget(P.tel, probe, "result").dumps(), probe
    # What is measured anew differs from load to load.
    again = P.telemetry_for(new_run(page, nav, ["frame", "ua", "globals"], "120:5"))
    assert again.json != tel.json
    changed = sum(
        1 for k, item in zip(got.keys, got.items) if jget(item, "duration_ms").text != jget(P.tel, k, "duration_ms").text
    )
    assert changed >= 10, "only %d probe durations differ from the recording" % changed
    assert telemetry_hash(tel.json) != P.recorded.tel_hash


def test_new_run_script_starts_after_the_page_came():
    """Python's own rule: the script starts no sooner than 30-90 ms after the
    page came (a slow proxy), and the rest of the timeline follows."""
    for _ in range(50):
        r = new_run("https://id.vk.com/x?session_token=T", 1800000000000, [], "", loaded_ms=1200)
        assert 1230 <= r.script_at < 1290
        tel = P.telemetry_for(r)
        assert abs(tel.ready_ms - r.script_at - tel.duration_ms) <= 1
    fast = [new_run("https://x/", 0, [], "", loaded_ms=50).script_at for _ in range(50)]
    assert min(fast) >= 300 and max(fast) < 450


def test_vary_probe_keeps_relations():
    """A varied probe keeps what ties its values together."""
    for _ in range(50):
        tel = P.tel.clone()
        warm = random.random() > 0.5
        for k, item in zip(tel.keys, tel.items):
            vary_probe(k, item, warm)

        def n(*path):
            return jnum(jget(tel, *path))[0]

        def near(what, a, b):
            assert abs(a - b) <= 0.051, "%s: %s, want %s" % (what, a, b)

        near("perf_reads - perf_zero", n("timer_resolution", "result", "perf_reads") - n("timer_resolution", "result", "perf_zero"), 200)
        near("visibility_raf", n("visibility_raf", "duration_ms") - n("visibility_raf", "result", "durationMs"), 0.1)
        if warm:
            assert n("media_devices", "duration_ms") <= 2.5
            assert n("speech_voices", "result", "waited_ms") <= 2
        else:
            near("speech_voices", n("speech_voices", "duration_ms") - n("speech_voices", "result", "waited_ms"), 0.9)
            near(
                "wasm",
                n("wasm", "duration_ms") - n("wasm", "result", "compileMs") - n("wasm", "result", "execMs"),
                0.1,
            )
        for k, item in zip(tel.keys, tel.items):
            d, ok = jnum(jget(item, "duration_ms"))
            assert ok and d >= 0, k


def test_json_tree():
    """A JSON value reads back byte for byte, and numbers are written as
    JavaScript writes them."""
    doc = '{"a":[1,2.5,-0.1,525.9000000003725,1e-7,"x\\"y",true,null],"b":{"c":"é😀","d":{}},"e":[]}'
    assert parse_json(doc).dumps() == doc
    for x, want in [
        (0, "0"), (-0.0, "0"), (2108, "2108"), (1793724460581, "1793724460581"), (-240, "-240"), (0.1, "0.1"),
        (0.30000000000000004, "0.30000000000000004"), (150.4, "150.4"), (1e-7, "1e-7"), (1.5e21, "1.5e+21"),
        (1e16, "10000000000000000"), (123456789012345680000, "123456789012345680000"), (1e21, "1e+21"),
        (2.5e-7, "2.5e-7"), (0.000001, "0.000001"),
    ]:
        assert js_number(x) == want, x
    with pytest.raises(ValueError):
        parse_json('{"a":1} x')
    with pytest.raises(ValueError):
        parse_json("NaN")


def test_go_round_is_half_away_from_zero():
    assert [go_round(x) for x in (0.5, 1.5, 2.5, -0.5, -2.5, 0.49999999999999994, 3.0)] == [1, 2, 3, -1, -3, 0, 3]
    assert round1(0.25) == 0.3 and round1(-0.25) == -0.3
    assert math.isnan(go_round(float("nan")))


def test_oracle_position():
    """The oracle's column is counted as V8 counts it, in UTF-16 code units."""
    html = "<html>\n<script>\nvar a='😀🎉';try{var s=String(new Error('oracle')['stack'])}catch(e){}\n</script>"
    # "var a='😀🎉';try{var s=String(" is 28 characters and 30 UTF-16 units.
    assert oracle_position(html) == "3:31"
    assert oracle_position("<html></html>") == ""


def test_pow_v2_envelope_is_ascii():
    """The page's script escapes every non-ASCII character before btoa, UTF-16
    surrogates above U+FFFF; tel_hash is over the UTF-8 canonical JSON."""
    tel = '{"b":{"ok":true,"result":"é😀"},"a":{"ok":true,"result":1.50}}'
    param = pow_v2("00ab", 7, 12, tel)
    raw = __import__("base64").b64decode(param[3:])
    assert raw.isascii()
    assert raw.decode() == (
        '{"hash":"00ab","nonce":7,"error":"","duration_ms":12,'
        '"telemetry":{"b":{"ok":true,"result":"\\u00e9\\ud83d\\ude00"},"a":{"ok":true,"result":1.50}},'
        '"tel_hash":"' + hashlib.sha256('{"a":{"ok":true,"result":1.50},"b":{"ok":true,"result":"é😀"}}'.encode()).hexdigest() + '"}'
    )
