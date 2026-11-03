"""MurmurHash3 x64 128-bit (port of fingerprintjs `x64hash128`)."""

_MASK64 = 0xFFFFFFFFFFFFFFFF
_C1 = 0x87C37B91114253D5
_C2 = 0x4CF5AD432745937F


def _rotl(x: int, r: int) -> int:
    return ((x << r) | (x >> (64 - r))) & _MASK64


def _fmix(k: int) -> int:
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & _MASK64
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & _MASK64
    k ^= k >> 33
    return k


def x64hash128(input_str: str, seed: int = 0) -> str:
    key = input_str.encode("utf-8")
    length = len(key)
    n_blocks = length // 16

    h1 = seed & _MASK64
    h2 = seed & _MASK64

    for i in range(n_blocks):
        off = i * 16
        k1 = int.from_bytes(key[off : off + 8], "little")
        k2 = int.from_bytes(key[off + 8 : off + 16], "little")

        k1 = (k1 * _C1) & _MASK64
        k1 = _rotl(k1, 31)
        k1 = (k1 * _C2) & _MASK64
        h1 ^= k1
        h1 = _rotl(h1, 27)
        h1 = (h1 + h2) & _MASK64
        h1 = (h1 * 5 + 0x52DCE729) & _MASK64

        k2 = (k2 * _C2) & _MASK64
        k2 = _rotl(k2, 33)
        k2 = (k2 * _C1) & _MASK64
        h2 ^= k2
        h2 = _rotl(h2, 31)
        h2 = (h2 + h1) & _MASK64
        h2 = (h2 * 5 + 0x38495AB5) & _MASK64

    tail = key[n_blocks * 16 :]
    rem = len(tail)
    if rem > 8:
        k2 = int.from_bytes(tail[8:], "little")
        k2 = (k2 * _C2) & _MASK64
        k2 = _rotl(k2, 33)
        k2 = (k2 * _C1) & _MASK64
        h2 ^= k2
    if rem > 0:
        k1 = int.from_bytes(tail[:8], "little")
        k1 = (k1 * _C1) & _MASK64
        k1 = _rotl(k1, 31)
        k1 = (k1 * _C2) & _MASK64
        h1 ^= k1

    h1 ^= length
    h2 ^= length
    h1 = (h1 + h2) & _MASK64
    h2 = (h2 + h1) & _MASK64
    h1 = _fmix(h1)
    h2 = _fmix(h2)
    h1 = (h1 + h2) & _MASK64
    h2 = (h2 + h1) & _MASK64

    return f"{h1:016x}{h2:016x}"
