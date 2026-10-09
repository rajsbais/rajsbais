"""Throughput models fitted from measured samples: seconds = a + b * rows, per (phase, environment).

Ordinary least squares with one robust pass (samples whose residual exceeds 3 standard errors are dropped), a prediction interval from the
fit, a leave-one-out error to say how well the model predicts data it has not seen, and explicit refusal to fit when the data cannot
support a model. No machine learning: a straight line through honest measurements, with its uncertainty stated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

MIN_SAMPLES, MIN_DISTINCT, MIN_SPAN = 5, 3, 2.0
# t quantile for an 80% two-sided prediction interval, by degrees of freedom
_T80 = {1: 3.078, 2: 1.886, 3: 1.638, 4: 1.533, 5: 1.476, 6: 1.440, 7: 1.415, 8: 1.397, 9: 1.383, 10: 1.372, 12: 1.356, 15: 1.341, 20: 1.325, 30: 1.310}


def t80(df: int) -> float:
    if df < 1:
        return float("inf")
    return _T80.get(df) or next((v for k, v in sorted(_T80.items(), reverse=True) if k <= df), 1.282)


class NoModel(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Model:
    a: float
    b: float
    n: int
    sigma: float
    sxx: float
    mean_x: float
    min_x: float
    max_x: float
    r2: float
    mape: float | None
    dropped: int

    @property
    def quality(self) -> str:
        if self.mape is None:
            return "unknown"
        return "good" if self.mape < 0.25 else "fair" if self.mape < 0.5 else "poor"

    @property
    def rows_per_second(self) -> float:
        return 1.0 / self.b if self.b > 0 else float("inf")

    def predict(self, rows: float) -> dict:
        pred = self.a + self.b * rows
        se = self.sigma * math.sqrt(1 + 1 / self.n + (rows - self.mean_x) ** 2 / self.sxx)
        extrapolated = rows > 2 * self.max_x or rows < self.min_x / 2
        half = t80(self.n - 2) * se * (1.5 if extrapolated else 1.0)
        return {"seconds": max(0.0, pred), "low": max(0.0, pred - half), "high": max(0.0, pred + half), "half_width": half, "extrapolated": extrapolated}

    def public(self) -> dict:
        return {"intercept_s": round(self.a, 6), "seconds_per_row": self.b, "rows_per_second": round(self.rows_per_second, 1), "n": self.n,
                "sigma_s": round(self.sigma, 6), "r2": round(self.r2, 4), "loo_mape": None if self.mape is None else round(self.mape, 4),
                "quality": self.quality, "rows_observed": [self.min_x, self.max_x], "dropped_outliers": self.dropped}


def _ols(xs: list[float], ys: list[float]) -> tuple[float, float, float, float]:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise NoModel("all samples have the same size")
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b, sxx, mx


def fit(points: list[tuple[float, float]]) -> Model:
    """points = [(rows, seconds)]. Raises NoModel with the reason the data cannot support a model."""
    pts = [(float(r), float(s)) for r, s in points if r > 0 and s >= 0]
    if len(pts) < MIN_SAMPLES:
        raise NoModel(f"{len(pts)} usable sample(s); at least {MIN_SAMPLES} are needed")
    if len({r for r, _ in pts}) < MIN_DISTINCT:
        raise NoModel(f"samples cover fewer than {MIN_DISTINCT} different sizes")
    if max(r for r, _ in pts) / min(r for r, _ in pts) < MIN_SPAN:
        raise NoModel(f"sizes span less than a factor of {MIN_SPAN:g}: the slope cannot be told from noise")
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    a, b, sxx, mx = _ols(xs, ys)
    dropped = 0
    n = len(xs)
    s = math.sqrt(sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys)) / max(1, n - 2))
    if s > 0:
        keep = [i for i in range(n) if abs(ys[i] - (a + b * xs[i])) <= 3 * s]
        if len(keep) < n and len(keep) >= MIN_SAMPLES and len({xs[i] for i in keep}) >= MIN_DISTINCT:
            dropped = n - len(keep)
            xs, ys = [xs[i] for i in keep], [ys[i] for i in keep]
            a, b, sxx, mx = _ols(xs, ys)
            n = len(xs)
    if b <= 0:
        raise NoModel("the fitted throughput is not positive: more rows did not take more time (measurements are too noisy or wrong)")
    sse = sum((y - (a + b * x)) ** 2 for x, y in zip(xs, ys))
    sigma = math.sqrt(sse / max(1, n - 2))
    my = sum(ys) / n
    sst = sum((y - my) ** 2 for y in ys)
    r2 = 1 - sse / sst if sst > 0 else 1.0
    errs = []
    for i in range(n):  # leave one out
        rest_x, rest_y = xs[:i] + xs[i + 1:], ys[:i] + ys[i + 1:]
        try:
            a2, b2, _, _ = _ols(rest_x, rest_y)
        except NoModel:
            continue
        if ys[i] > 1e-6:
            errs.append(abs((a2 + b2 * xs[i]) - ys[i]) / ys[i])
    mape = sum(errs) / len(errs) if errs else None
    return Model(a, b, n, sigma, sxx, mx, min(xs), max(xs), r2, mape, dropped)
