"""A JSON tree that keeps object keys in order and numbers as written.

What the solver does not change in the browser's telemetry goes out byte for
byte as the browser wrote it (port of ``jsontree.go``). Also here: numbers,
strings and rounding as JavaScript and Go write them.
"""

import json.decoder
import math
import re
from decimal import Decimal
from typing import Iterator, List, Optional, Tuple

OBJECT, ARRAY, STRING, LITERAL = range(4)


class JSONValue:
    """A JSON value: an object (keys and items in order), an array (items), a
    string (text is its value) or a literal (text is a number, true, false or
    null as written)."""

    __slots__ = ("kind", "text", "keys", "items", "span")

    def __init__(
        self,
        kind: int,
        text: str = "",
        keys: Optional[List[str]] = None,
        items: Optional[List["JSONValue"]] = None,
    ) -> None:
        self.kind = kind
        self.text = text
        self.keys: List[str] = keys if keys is not None else []
        self.items: List["JSONValue"] = items if items is not None else []
        # span is where the value stood in the text it was read from.
        self.span: Tuple[int, int] = (0, 0)

    # -- writing -------------------------------------------------------------

    def dumps(self) -> str:
        """The value as JSON.stringify writes it: compact, keys in order."""
        out: List[str] = []
        self._write(out)
        return "".join(out)

    def _write(self, out: List[str]) -> None:
        if self.kind == OBJECT:
            out.append("{")
            for i, (k, item) in enumerate(zip(self.keys, self.items)):
                if i:
                    out.append(",")
                out.append(js_string(k))
                out.append(":")
                item._write(out)
            out.append("}")
        elif self.kind == ARRAY:
            out.append("[")
            for i, item in enumerate(self.items):
                if i:
                    out.append(",")
                item._write(out)
            out.append("]")
        elif self.kind == STRING:
            out.append(js_string(self.text))
        else:
            out.append(self.text)

    # -- reading and changing ------------------------------------------------

    def clone(self) -> "JSONValue":
        c = JSONValue(self.kind, self.text, list(self.keys), [i.clone() for i in self.items])
        c.span = self.span
        return c

    def assign(self, other: "JSONValue") -> None:
        """Make this value other, in place (Go's ``*item = jsonValue{...}``)."""
        self.kind, self.text = other.kind, other.text
        self.keys, self.items = list(other.keys), list(other.items)

    def reorder(self, keys: List[str]) -> None:
        """Put the object's keys in the order of keys; keys it lacks are
        skipped, and its keys missing from keys keep their order after the rest."""
        taken = [False] * len(self.keys)
        new_keys: List[str] = []
        new_items: List[JSONValue] = []
        for k in keys:
            i = index_of(self.keys, k)
            if i >= 0 and not taken[i]:
                taken[i] = True
                new_keys.append(k)
                new_items.append(self.items[i])
        for i, k in enumerate(self.keys):
            if not taken[i]:
                new_keys.append(k)
                new_items.append(self.items[i])
        self.keys, self.items = new_keys, new_items

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "JSONValue(%s)" % self.dumps()[:80]


def literal(text: str) -> JSONValue:
    return JSONValue(LITERAL, text)


def jget(v: Optional[JSONValue], *path: str) -> Optional[JSONValue]:
    """Follow object keys from v; None when one is missing."""
    for key in path:
        if v is None or v.kind != OBJECT:
            return None
        i = index_of(v.keys, key)
        if i < 0:
            return None
        v = v.items[i]
    return v


def jnum(v: Optional[JSONValue]) -> Tuple[float, bool]:
    """v as a number; (0, False) when v is not one."""
    if v is None or v.kind != LITERAL or v.text in ("true", "false", "null"):
        return 0.0, False
    try:
        return float(v.text), True
    except ValueError:
        return 0.0, False


def jset_num(v: Optional[JSONValue], key: str, x: float) -> None:
    """Put x at key of the object v, written as JavaScript writes a number; a
    missing key (or v None) is left missing."""
    item = jget(v, key)
    if item is not None:
        item.assign(literal(js_number(x)))


def jset_str(v: Optional[JSONValue], key: str, s: str) -> None:
    """Put s at key of the object v; a missing key is left missing."""
    item = jget(v, key)
    if item is not None:
        item.assign(JSONValue(STRING, s))


def index_of(xs: List[str], x: str) -> int:
    for i, s in enumerate(xs):
        if s == x:
            return i
    return -1


# -- parsing -----------------------------------------------------------------

_WS = " \t\n\r"
_NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_scanstring = json.decoder.scanstring


def parse_json(text: str) -> JSONValue:
    """Read one JSON value; nothing but whitespace may follow it."""
    v, i = _parse_value(text, _skip(text, 0))
    if _skip(text, i) != len(text):
        raise ValueError("data after the JSON value")
    return v


def _skip(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i] in _WS:
        i += 1
    return i


def _parse_value(s: str, i: int) -> Tuple[JSONValue, int]:
    if i >= len(s):
        raise ValueError("unexpected end of JSON")
    c = s[i]
    start = i
    if c == "{":
        v = JSONValue(OBJECT)
        i = _skip(s, i + 1)
        if i < len(s) and s[i] == "}":
            i += 1
        else:
            while True:
                if i >= len(s) or s[i] != '"':
                    raise ValueError("object key expected at %d" % i)
                key, i = _scanstring(s, i + 1, True)
                i = _skip(s, i)
                if i >= len(s) or s[i] != ":":
                    raise ValueError("':' expected at %d" % i)
                item, i = _parse_value(s, _skip(s, i + 1))
                v.keys.append(key)
                v.items.append(item)
                i = _skip(s, i)
                if i < len(s) and s[i] == ",":
                    i = _skip(s, i + 1)
                    continue
                if i < len(s) and s[i] == "}":
                    i += 1
                    break
                raise ValueError("',' or '}' expected at %d" % i)
    elif c == "[":
        v = JSONValue(ARRAY)
        i = _skip(s, i + 1)
        if i < len(s) and s[i] == "]":
            i += 1
        else:
            while True:
                item, i = _parse_value(s, i)
                v.items.append(item)
                i = _skip(s, i)
                if i < len(s) and s[i] == ",":
                    i = _skip(s, i + 1)
                    continue
                if i < len(s) and s[i] == "]":
                    i += 1
                    break
                raise ValueError("',' or ']' expected at %d" % i)
    elif c == '"':
        text, i = _scanstring(s, i + 1, True)
        v = JSONValue(STRING, text)
    else:
        for word in ("true", "false", "null"):
            if s.startswith(word, i):
                v = literal(word)
                i += len(word)
                break
        else:
            m = _NUMBER_RE.match(s, i)
            if not m:
                raise ValueError("unexpected character at %d" % i)
            v = literal(m.group(0))
            i = m.end()
    v.span = (start, i)
    return v, i


def to_python(v: JSONValue):
    """The value as plain Python data (dict, list, str, int, float, bool, None)."""
    if v.kind == OBJECT:
        return {k: to_python(item) for k, item in zip(v.keys, v.items)}
    if v.kind == ARRAY:
        return [to_python(item) for item in v.items]
    if v.kind == STRING:
        return v.text
    if v.text == "true":
        return True
    if v.text == "false":
        return False
    if v.text == "null":
        return None
    if re.fullmatch(r"-?[0-9]+", v.text):
        return int(v.text)
    return float(v.text)


# -- canonical JSON ------------------------------------------------------------


def canonical_js(v: JSONValue) -> str:
    """v as the page's script canonicalises the telemetry: object keys sorted
    (a repeated key keeps its last value, as a decoder does), nothing between
    tokens, numbers as written, strings as JSON.stringify writes them."""
    out: List[str] = []
    _canonical(v, out)
    return "".join(out)


def _canonical(v: JSONValue, out: List[str]) -> None:
    if v.kind == OBJECT:
        last = {}
        for k, item in zip(v.keys, v.items):
            last[k] = item
        out.append("{")
        for i, k in enumerate(sorted(last, key=lambda s: s.encode("utf-8", "surrogatepass"))):
            if i:
                out.append(",")
            out.append(js_string(k))
            out.append(":")
            _canonical(last[k], out)
        out.append("}")
    elif v.kind == ARRAY:
        out.append("[")
        for i, item in enumerate(v.items):
            if i:
                out.append(",")
            _canonical(item, out)
        out.append("]")
    elif v.kind == STRING:
        out.append(js_string(v.text))
    else:
        out.append(v.text)


# -- JavaScript's writing of numbers and strings -------------------------------

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_NEEDS_ESCAPE = re.compile(r'["\\\x00-\x1f]')


def _escape(m: "re.Match") -> str:
    c = m.group(0)
    return _ESCAPES.get(c) or "\\u%04x" % ord(c)


def js_string(s: str) -> str:
    """s as JSON.stringify writes a string: quotes, backslashes and control
    characters escaped, everything else as is."""
    return '"' + _NEEDS_ESCAPE.sub(_escape, s) + '"'


def ascii_json(s: str) -> str:
    """Every non-ASCII character of s as its \\uXXXX escape, UTF-16 surrogates
    for those above U+FFFF, as the page's script does before btoa."""
    out = []
    for ch in s:
        r = ord(ch)
        if r < 0x80:
            out.append(ch)
        elif r > 0xFFFF:
            r -= 0x10000
            out.append("\\u%04x\\u%04x" % (0xD800 + (r >> 10), 0xDC00 + (r & 0x3FF)))
        else:
            out.append("\\u%04x" % r)
    return "".join(out)


def js_number(x: float) -> str:
    """x as JavaScript's Number.prototype.toString writes it: the shortest
    digits that read back as x, an exponent only below 1e-6 or from 1e21 on."""
    x = float(x)
    if x == 0 or math.isnan(x) or math.isinf(x):
        # The telemetry has none of the last two; JSON.stringify writes them as null.
        return "0"
    r = repr(x)
    a = abs(x)
    if 1e-6 <= a < 1e21:
        s = format(Decimal(r), "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s
    mant, _, exp = r.partition("e")  # 1.5e-07, 1.5e+21
    sign, digits = exp[0], exp[1:].lstrip("0")
    return mant + "e" + sign + digits


def go_round(x: float) -> float:
    """Go's math.Round: half away from zero (Python's round() goes to even)."""
    if math.isnan(x) or math.isinf(x):
        return x
    t = float(math.trunc(x))
    if abs(x - t) >= 0.5:
        t += math.copysign(1.0, x)
    return math.copysign(t, x)


def round1(x: float) -> float:
    return go_round(x * 10) / 10


def iter_items(v: Optional[JSONValue]) -> Iterator[JSONValue]:
    if v is not None:
        yield from v.items
