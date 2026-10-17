"""Shadow thermal predictor for the Thermal Battery app (no AppDaemon deps).

Per zone, on 30-min steps:

    T[t+1] - T[t] = a*(Tout - T) + m*(M - T) + b*I + bs*S + c*q + ch*H + d

    T     room air (median of the step's samples)
    Tout  outdoor air (Open-Meteo, interpolated)
    I     sun on the window plane, kW/m2 (global_tilted_irradiance)
    q     heat delivered to the zone in the step, (C*min)/100
    S, H  exponentially filtered I and q (slab / room mass memory)
    M     latent mass temperature: slow (TAU_MASS_H) average of air temp.
          Sun lifts the air fast but the slab soaks most of it up, so after
          sunset the air drops quickly back toward M and then flattens.
          The settle term m*(M - T) absorbs that fast drop so it isn't
          mistaken for loss to outdoors (which would be extrapolated all
          night and over-predict the morning drop).

Coefficients are response ratios (UA/C, s/C, ...), not physical UA or C,
which can't be separated from temperature data. Fit by ridge least squares
on one-step changes; predictions are free-run simulations. Pure Python.
"""

import bisect
import math

STEP = 1800
TAU_H = 6.0
TAU_MASS_H = 24.0
NAMES = ("loss", "settle", "sun", "sun_mem", "heat", "heat_mem", "bias")


def _median(v):
    v = sorted(v)
    n = len(v)
    if not n:
        return None
    return v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2])


def _interp(times, vals, t):
    i = bisect.bisect_right(times, t) - 1
    if i < 0 or i + 1 >= len(times):
        return None
    a, b = vals[i], vals[i + 1]
    if a is None or b is None:
        return None
    return a + (b - a) * (t - times[i]) / float(times[i + 1] - times[i])


def build_steps(samples, weather, start, end, sample_min=1.0):
    """samples: [(ts, air, calling, supply, ret, away[, outdoor])] sorted by
    ts. weather: [(hour_ts, temp, gti)] sorted. Steps cover [start, end).
    Tout is the measured outdoor median when the step has >= 3 readings,
    else the (forecast/analysis) weather value."""
    wt = [w[0] for w in weather]
    wT = [w[1] for w in weather]
    wI = [w[2] for w in weather]
    stimes = [s[0] for s in samples]
    steps = []
    t = int(start // STEP * STEP)
    while t < end:
        lo = bisect.bisect_left(stimes, t)
        hi = bisect.bisect_left(stimes, t + STEP)
        chunk = samples[lo:hi]
        airs = [s[1] for s in chunk if s[1] is not None]
        outs = [s[6] for s in chunk if len(s) > 6 and s[6] is not None]
        q = 0.0
        away = True
        for s in chunk:
            if s[2] and s[3] is not None and s[4] is not None:
                q += max(0.0, s[3] - s[4]) * sample_min
            if s[5] is not None and not s[5]:
                away = False
        gti = _interp(wt, wI, t + STEP / 2)
        steps.append({
            "t": t,
            "T": _median(airs) if len(airs) >= 3 else None,
            "Tout": (_median(outs) if len(outs) >= 3
                     else _interp(wt, wT, t + STEP / 2)),
            "I": (gti or 0.0) / 1000.0,
            "q": q / 100.0,
            "away": away,
        })
        t += STEP
    return steps


def _mass_a():
    return math.exp(-(STEP / 3600.0) / TAU_MASS_H)


def filter_memory(steps, S=0.0, H=0.0, M=None, tau_h=TAU_H):
    """Adds filtered sun/heat ('S', 'H') and the latent mass temp ('M', from
    measured air); returns the end state (S, H, M)."""
    a = math.exp(-(STEP / 3600.0) / tau_h)
    am = _mass_a()
    for r in steps:
        S = a * S + (1 - a) * r["I"]
        H = a * H + (1 - a) * r["q"]
        r["S"], r["H"] = S, H
        T = r.get("T")
        r["M"] = M if M is not None else T
        if T is not None:
            M = T if M is None else am * M + (1 - am) * T
    return S, H, M


def _x(T, r, M):
    return [r["Tout"] - T, M - T, r["I"], r["S"], r["q"], r["H"], 1.0]


def _solve(A, b):
    n = len(b)
    M = [A[i][:] + [b[i]] for i in range(n)]
    for i in range(n):
        p = max(range(i, n), key=lambda k: abs(M[k][i]))
        M[i], M[p] = M[p], M[i]
        if abs(M[i][i]) < 1e-12:
            return None
        for k in range(n):
            if k != i:
                f = M[k][i] / M[i][i]
                M[k] = [M[k][j] - f * M[i][j] for j in range(n + 1)]
    return [M[i][n] / M[i][i] for i in range(n)]


def fit(steps, lam=0.05):
    """-> (coef list, n_pairs). coef None if not solvable."""
    n = len(NAMES)
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    cnt = 0
    for r0, r1 in zip(steps, steps[1:]):
        if (r0["T"] is None or r1["T"] is None or r0["Tout"] is None
                or r0.get("M") is None or not r0["away"] or not r1["away"]):
            continue
        x = _x(r0["T"], r0, r0["M"])
        y = r1["T"] - r0["T"]
        for i in range(n):
            b[i] += x[i] * y
            for j in range(n):
                A[i][j] += x[i] * x[j]
        cnt += 1
    if cnt < 10:
        return None, cnt
    for i in range(n - 1):  # ridge on everything but the bias
        A[i][i] += lam * cnt * (0.01 if i < 2 else 1.0)
    return _solve(A, b), cnt


def plausible(coef):
    """Physical sanity: decays toward outdoor temp, sun/heat don't cool."""
    if not coef:
        return False
    loss, settle, sun, sun_mem, heat, heat_mem = coef[:6]
    return (0.0 < loss < 0.5 and 0.0 <= settle < 1.0
            and sun + sun_mem >= -0.01 and heat + heat_mem >= -0.01)


def simulate(coef, T0, steps, M0=None):
    """Free-run from T0 (mass M0) at steps[0]['t']; returns [T per step]."""
    T = T0
    M = T0 if M0 is None else M0
    am = _mass_a()
    out = []
    for r in steps:
        out.append(T)
        if r["Tout"] is not None:
            Tn = T + sum(c * x for c, x in zip(coef, _x(T, r, M)))
            M = am * M + (1 - am) * T
            T = Tn
    return out


def robust_low(steps, start, end):
    """Min of the 30-min medians whose step starts in [start, end)."""
    vals = [r["T"] for r in steps if start <= r["t"] < end
            and r["T"] is not None]
    return min(vals) if vals else None
