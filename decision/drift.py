"""Lightweight ADWIN-style change detector for strategy health series."""
from __future__ import annotations

import math
from collections import deque


class ADWIN:
    """Variable-window mean-shift detector.

    This is a compact implementation of the ADWIN idea (adaptive window +
    Hoeffding-style split test), not a drop-in replacement for river's full
    implementation.  It is used for alerting/de-risking, not as a predictive
    model, so a conservative, auditable split test is sufficient.
    """

    def __init__(self, delta: float = 0.002, min_window: int = 12,
                 max_window: int = 2000):
        self.delta = max(1e-6, float(delta))
        self.min_window = max(4, int(min_window))
        self.max_window = max(self.min_window * 3, int(max_window))
        self.values: deque[float] = deque(maxlen=self.max_window)
        self.drift_count = 0
        self.last_split: dict | None = None

    def add(self, value: float) -> bool:
        value = float(value)
        if not math.isfinite(value):
            return False
        self.values.append(value)
        n = len(self.values)
        if n < self.min_window * 2:
            return False
        ordered = list(self.values)
        prefix = [0.0]
        for item in ordered:
            prefix.append(prefix[-1] + item)
        for split in range(self.min_window, n - self.min_window + 1):
            n0, n1 = split, n - split
            mean0 = (prefix[split] - prefix[0]) / n0
            mean1 = (prefix[n] - prefix[split]) / n1
            m = 1.0 / (1.0 / n0 + 1.0 / n1)
            delta_prime = self.delta / max(1.0, math.log(max(2, n)))
            epsilon = math.sqrt(math.log(4.0 / delta_prime) / (2.0 * m))
            if abs(mean0 - mean1) > epsilon:
                self.drift_count += 1
                self.last_split = {
                    "split": split, "n0": n0, "n1": n1,
                    "mean0": round(mean0, 6), "mean1": round(mean1, 6),
                    "threshold": round(epsilon, 6),
                }
                # Drop the old concept window but retain the recent side.
                keep = ordered[split:]
                self.values.clear()
                self.values.extend(keep)
                return True
        return False
