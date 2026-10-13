"""Free-space trend and a time-to-full estimate that does not jump around.

Builds delete and rewrite tens of GB, so free space is a sawtooth. The estimate works on the
lowest free space per 10 minutes (the "7d" history ring, which aggregates by minimum), fits a
median-of-slopes trend over several windows, and only moves its displayed value when the
underlying estimate has clearly changed.
"""

GB = 1 << 30
MB = 1 << 20
WINDOWS = ((3600, 5 * GB), (6 * 3600, 2 * GB), (24 * 3600, 2 * GB))   # (span seconds, minimum drop)
STEPS_H = (1, 2, 3, 4, 6, 8, 12, 18, 24, 36, 48, 72, 96, 120, 168)


def theil_sen(points):
    """[(t, v)] -> (median slope, share of pairwise slopes that are negative)."""
    slopes = []
    n = len(points)
    for i in range(n):
        ti, vi = points[i]
        for j in range(i + 1, n):
            tj, vj = points[j]
            if tj != ti:
                slopes.append((vj - vi) / (tj - ti))
    if not slopes:
        return 0.0, 0.0
    slopes.sort()
    m = len(slopes)
    med = slopes[m // 2] if m % 2 else (slopes[m // 2 - 1] + slopes[m // 2]) / 2
    return med, sum(1 for s in slopes if s < 0) / m


def loss_rate(troughs, total_b):
    """Fastest credible rate of space loss in bytes/second, or None if the disk looks steady.

    troughs: [(ts, avail_bytes)] oldest -> newest, one per 10 minutes, gaps omitted.
    """
    if len(troughs) < 4:
        return None
    newest = troughs[-1][0]
    best = None
    for span, min_drop in WINDOWS:
        pts = [p for p in troughs if p[0] >= newest - span]
        covered = pts[-1][0] - pts[0][0] if pts else 0
        if len(pts) < 4 or covered < span * 0.5:
            continue
        slope, neg_share = theil_sen(pts)
        drop = -slope * covered
        if neg_share >= 0.7 and drop >= max(min_drop, 0.005 * total_b):
            rate = -slope
            if best is None or rate > best:
                best = rate
    return best


def nearest_step(hours):
    return min(STEPS_H, key=lambda s: abs(s - hours) / s)


class DiskTrend:
    """Per-mount estimate with hysteresis on the value that is shown."""

    def __init__(self):
        self.shown_h = None       # displayed step, hours
        self.strikes = 0          # consecutive evaluations that disagreed with shown_h
        self.rate_bps = None

    def update(self, troughs, total_b, floor_b, crit_h=8, warn_h=48):
        """Re-evaluate. floor_b is the lowest free space of the last 30 minutes.
        Returns (eta_seconds or None, loss rate bytes/s or None)."""
        rate = loss_rate(troughs, total_b)
        self.rate_bps = rate
        if not rate or rate <= 0:
            self.shown_h, self.strikes = None, 0
            return None, None
        raw_h = floor_b / rate / 3600.0
        if raw_h > STEPS_H[-1] * 1.4:
            self.shown_h, self.strikes = None, 0      # beyond a week: not worth a number
            return None, rate
        if self.shown_h is None:
            self.shown_h, self.strikes = nearest_step(raw_h), 0
        else:
            worse = ((raw_h < crit_h <= self.shown_h) or (raw_h < warn_h <= self.shown_h))
            inside = 0.7 * self.shown_h <= raw_h <= 1.4 * self.shown_h
            if worse:
                self.shown_h, self.strikes = nearest_step(raw_h), 0
            elif inside:
                self.strikes = 0
            else:
                self.strikes += 1
                if self.strikes >= 2:
                    self.shown_h, self.strikes = nearest_step(raw_h), 0
        return self.shown_h * 3600, rate


def troughs_from_ring(ring, now):
    """10-minute minimum free space from a history ring -> [(ts, bytes)], gaps dropped."""
    if ring is None:
        return []
    vals, t0 = ring.ordered(now)
    step = ring.step
    return [(t0 + i * step, v * MB) for i, v in enumerate(vals) if v == v]


def change_over(ring, now, span_s, current_b):
    """Bytes gained (+) or lost (-) over span_s, or None if history does not reach that far."""
    if ring is None:
        return None
    then = ring.value_at(now - span_s)
    if then is None:
        return None
    return int(current_b - then * MB)
