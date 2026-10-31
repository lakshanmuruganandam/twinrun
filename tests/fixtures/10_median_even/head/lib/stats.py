"""Small statistics helpers."""


def mean(xs: list[float]) -> float:
    if not xs:
        raise ValueError("mean of empty list")
    return sum(xs) / len(xs)


def median(xs: list[int]) -> float:
    if not xs:
        raise ValueError("median of empty list")
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return float(s[mid])


def clamp(x: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, x))


def histogram(values: list[int], bucket: int = 10) -> dict[int, int]:
    out = {}
    for v in values:
        k = (v // bucket) * bucket
        out[k] = out.get(k, 0) + 1
    return out


def moving_average(xs: list[float], window: int = 3) -> list[float]:
    if window <= 0:
        raise ValueError("window must be positive")
    return [sum(xs[i:i + window]) / window for i in range(len(xs) - window + 1)]
