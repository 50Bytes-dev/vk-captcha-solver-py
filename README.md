# VK Captcha Solver

Solves the VK ID captcha ("Я не робот"):

- the captcha page VK links in `redirect_uri` of API error 14;
- the validation page of error 17 (`m.vk.com/activation?act=validate…`): the
  solver opens it as m.vk.com's own page asks for it, solves the captcha
  behind it and posts the `success_token` to the page's form.

Both kinds of captcha page are read:

- the **BFF page** VK rolled out in the autumn of 2026 (proof of work v2,
  `captchaNotRobot.initSession`);
- the **old page** (`const powInput`, `window.init`).

A checkbox is answered with a cursor trace, a slider by finding how many of
VK's tile swaps put the picture back together. When VK answers a checkbox
`BOT` and offers a slider, the solver goes on to the slider, as VK's widget
does.

## Use

```python
from vk_captcha_solver import CaptchaSolver, failure_kind

solver = CaptchaSolver()            # one per process is enough; safe for concurrent use

token = await solver.solve(redirect_uri, proxy="http://user:pass@host:3128")
token, report = await solver.solve_report(redirect_uri)
await solver.solve_validation(validation_redirect_uri)

# vkbottle
api.add_captcha_handler(solver.vkbottle_captcha_handler)
api.add_validation_handler(solver.vkbottle_validation_handler)

# 0.1 style, still works
async with solver.session(proxy="http://proxy:8080"):
    token = await solver.solve(redirect_uri)
```

Every solving has its own session and cookie jar. A failure raises
`VKCaptchaSolverError` (or its `APIError`, `HTTPError`). The error carries
`.report`, how far the solving got. `failure_kind(err)` names its kind.

Error texts hold no URL, page body or proxy. A page that lacked a value is in
`err.url` and `err.body`.

The log is `logging.getLogger("vk_captcha_solver")`:

- one INFO line per solved captcha;
- one WARNING per failed one;
- DEBUG lines for the steps, with hosts and paths only.

## Options

`CaptchaSolver(api_options)`; every key is optional.

| Key | Default | What |
|---|---|---|
| `timeout` | 60 | seconds the whole solving may take; `solve(..., timeout=)` overrides it |
| `pageTimeout` | 10 | seconds for one try at the captcha page; a hang, a network failure or a 5xx gets one more try |
| `requestTimeout` | 30 | seconds for one request |
| `proxy` | none | proxy of a solving that names none: `http`, `https`, `socks5`, `socks5h` |
| `userAgent` | the profile's | a different one also drops the profile's client hints |
| `headers`, `cookies` | none | added to every request |
| `timezone` | see below | IANA name the browser claims |
| `baseUrl`, `version` | `https://api.vk.ru`, `5.199` | API of the old page; the BFF page always calls `api.vk.com`, `v=5.131` |

The time zone is taken from the option, then `VK_CAPTCHA_TIMEZONE`, then `TZ`
when it names a zone, else `Europe/Moscow`. The offset is computed for that
zone.

## Failure kinds

| Kind | Meaning |
|---|---|
| `timeout` | the network or VK was slow (a 5xx), or the time ran out; no check is sent sooner than a person could send it |
| `bot` | VK's antibot answered `BOT`: it no longer takes the browser profile |
| `page_changed` | the page or its script lacks what the solver reads, its probes are not the profile's, or the captcha is of an unknown kind |
| `failed` | anything else, e.g. a slider answer VK did not take |

## The browser profile

The BFF page's proof of work carries a browser's telemetry. The solver sends
one recorded browser: `vk_captcha_solver/profiles/chrome156-macos.json`
(Chrome 156 on macOS, recorded on id.vk.com on 2026-11-03). That file holds
the telemetry, device, fingerprint and client hints of that browser.

For each load the solver:

- puts the probes in the order the page asks for;
- varies what a browser measures anew;
- sends the check no sooner than a person would.

The profile knows two versions of the page's proof of work script:
`recorded-43` and `devtools-37`.

The profile goes stale when VK changes the script or stops trusting the
browser. **`bot` or `page_changed` means a new recording is needed.**

## Credits

- A port of [@tripoloski/vk-captcha-solver](https://github.com/tripoloski-it/vk-captcha-solver)
  0.0.3 (ISC).
- The BFF page, the browser profile and the timing come from the Go port of
  this package (`vkapi/vkcaptcha`), ported back here.

## Development

```sh
uv run pytest
```

The tests reach no network: VK's hosts resolve to a local TLS server.
