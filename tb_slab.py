"""Two-node (air + slab) zone model with a Kalman filter (no AppDaemon deps).

Per zone, continuous time (rates per hour):

    dTa/dt = kas*(Ts - Ta) + kao*(Tout - Ta) + sa*I
    dTs/dt = r*kas*(Ta - Ts) + ksg*(TG - Ts) + ss*I     (+ h*q as an impulse)

    Ta    room air (thermostat)
    Ts    slab ("how much energy is in the floor"), hidden
    Tout  outdoor air, I sun on the window plane (kW/m2)
    q     heat put into the loop in the step: sum((supply - return) * min)/100
          over the whole call (water that ends up hot in the pipes still
          ends up in the slab). It goes into a "tube" store E first and
          reaches the slab with a TAU_Q_H lag: the floor sensors peak about
          an hour after a call ends.
    TG    ground / below-grade temperature (fixed)

The slab is never measured directly in most zones, so a Kalman filter keeps
a running estimate from everything that sees it:

    air temperature           every step
    floor surface sensors     where a zone has them
    return-water dip          first minutes of every call: the water that sat
                              in the loop comes back at slab temperature
                              before the hot front has made it round (daily
                              probe runs make sure every zone gets one)

Each call also yields the loop transit time (call start to the steepest rise
of the return, i.e. when the hot front reaches the return probe); it bounds
where the dip can be read and is logged per zone.

Parameters are fitted per zone by maximum likelihood over the filter's
innovations with log-normal priors (so a zone with little heat data stays
near physically sensible values). Pure Python.
"""

import bisect
import math

STEP = 1800
SUB = 3                       # Euler substeps per step
TG = 10.0
TAU_Q_H = 1.0                 # tube -> slab lag
DIP_IDLE_MIN = 60             # loop idle this long before a call => dip valid
DIP_WINDOW_MIN = 15           # search for the dip in the first N min of a call
FRONT_PLATEAU_MIN = 25        # plateau = median return after this
FRONT_SEARCH_MIN = 30         # look for the hot front within this
NAMES = ("kas", "kao", "r", "ksg", "h", "sa", "ss")
PRIOR = {                     # (median, log-sd)
    "kas": (0.4, 1.0),
    "kao": (0.03, 1.2),
    "r": (0.15, 1.0),
    "ksg": (0.003, 0.7),
    "h": (0.3, 0.7),
    "sa": (1.0, 1.0),
    "ss": (0.1, 0.7),
}
Q_AIR = 0.04 ** 2             # process noise per step
Q_SLAB = 0.015 ** 2
R_AIR = 0.15 ** 2             # thermostats report in ~0.5 C (1 F) steps
R_AIR_COARSE = 0.2 ** 2       # ...and some in whole degF converted
R_FLOOR = 0.15 ** 2
R_DIP = 0.3 ** 2
# Zones with no floor sensor and no dip yet: a weak "slab ~ air" pseudo
# reading once the loop has been idle this long, so the unobserved slab
# can't drift into a free fudge state (seen: bunk slab 3 C above air after
# 10 days without a call).
PSEUDO_IDLE_H = 24
R_PSEUDO = 1.5 ** 2


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


# ------------------------------------------------------------- calls ------
def call_signatures(samples):
    """samples: [(ts, air, calling, supply, ret, away, outdoor, floor)]
    sorted. -> [{t0, dip, t_dip, t_front, plateau, valid}] per call start.
    valid = the loop was idle long enough for the dip to read the slab."""
    out = []
    last_call = None
    n = len(samples)
    for i, s in enumerate(samples):
        if not s[2]:
            continue
        if i and samples[i - 1][2]:
            last_call = s[0]
            continue
        t0 = s[0]
        valid = last_call is None or t0 - last_call >= DIP_IDLE_MIN * 60
        if last_call is None and samples[0][0] > t0 - DIP_IDLE_MIN * 60:
            valid = False   # can't see far enough back
        last_call = t0
        run = []
        for r in samples[i:]:
            if not r[2] or r[0] - t0 > 60 * 60:
                break
            if r[4] is not None:
                run.append(r)
        early = [r for r in run if r[0] - t0 <= DIP_WINDOW_MIN * 60]
        if len(early) < 3:
            continue
        d = min(early, key=lambda r: r[4])
        later = [r for r in run if r[0] > d[0]]
        plat = _median([r[4] for r in later
                        if r[0] - t0 >= FRONT_PLATEAU_MIN * 60])
        # short calls (probe runs) end before the plateau window: use the
        # warmest return seen so the hot front can still be found
        ref = plat if plat is not None else max(
            [r[4] for r in later], default=None)
        front = None
        if ref is not None and ref - d[4] > 2.0:
            # hot front = steepest return rise. A 50%-to-plateau rule reads
            # late: the return keeps creeping up as the whole loop warms.
            best = 0.0
            prev = d
            for r in later:
                if r[0] - t0 > FRONT_SEARCH_MIN * 60:
                    break
                rate = (r[4] - prev[4]) / max(1.0, r[0] - prev[0])
                if rate > best:
                    best = rate
                    front = 0.5 * (prev[0] + r[0])
                prev = r
        # a real dip: return fell well below where it sat before the call
        before = samples[i - 1][4] if i and samples[i - 1][4] is not None \
            else None
        if before is not None and before - d[4] < 1.0:
            valid = False
        out.append({
            "t0": t0, "dip": d[4], "t_dip": (d[0] - t0) / 60.0,
            "t_front": (front - t0) / 60.0 if front else None,
            "plateau": plat, "valid": bool(valid and front is not None),
        })
    return out


# ------------------------------------------------------------- steps ------
def build_steps(samples, weather, start, end, sample_min=1.0):
    """samples: [(ts, air, calling, supply, ret, away, outdoor, floor)].
    weather: [(hour_ts, temp, gti)]. Steps cover [start, end)."""
    wt = [w[0] for w in weather]
    wT = [w[1] for w in weather]
    wI = [w[2] for w in weather]
    stimes = [s[0] for s in samples]
    dips = {}
    for c in call_signatures(samples):
        if c["valid"]:
            dips[int(c["t0"] // STEP * STEP)] = c["dip"]
    steps = []
    t = int(start // STEP * STEP)
    while t < end:
        lo = bisect.bisect_left(stimes, t)
        hi = bisect.bisect_left(stimes, t + STEP)
        chunk = samples[lo:hi]
        airs = [s[1] for s in chunk if s[1] is not None]
        outs = [s[6] for s in chunk if s[6] is not None]
        floors = [s[7] for s in chunk if len(s) > 7 and s[7] is not None]
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
            "F": _median(floors) if len(floors) >= 3 else None,
            "D": dips.get(t),
            "Tout": (_median(outs) if len(outs) >= 3
                     else _interp(wt, wT, t + STEP / 2)),
            "I": (gti or 0.0) / 1000.0,
            "q": q / 100.0,
            "away": away,
        })
        t += STEP
    return steps


# ------------------------------------------------------------- model ------
def _mm(A, B):
    return [[sum(A[i][k] * B[k][j] for k in range(len(B)))
             for j in range(len(B[0]))] for i in range(len(A))]


def discretize(p):
    """-> (A 2x2, B 2x3) for x' = A x + B [Tout, I, 1] over one STEP."""
    dt = STEP / 3600.0 / SUB
    kas, kao, r, ksg, sa, ss = (p["kas"], p["kao"], p["r"], p["ksg"],
                                p["sa"], p["ss"])
    As = [[1 - dt * (kas + kao), dt * kas],
          [dt * r * kas, 1 - dt * (r * kas + ksg)]]
    Gs = [[dt * kao, dt * sa, 0.0],
          [0.0, dt * ss, dt * ksg * TG]]
    A = [[1.0, 0.0], [0.0, 1.0]]
    B = [[0.0] * 3, [0.0] * 3]
    for _ in range(SUB):
        A = _mm(As, A)
        B = [[a + g for a, g in zip(ra, rg)] for ra, rg in
             zip(_mm(As, B), Gs)]
    return A, B


_AQ = math.exp(-(STEP / 3600.0) / TAU_Q_H)


def _step(A, B, x, r, h, E):
    """-> (new [Ta, Ts], new tube store E)."""
    u = (r["Tout"], r["I"], 1.0)
    E += r["q"]
    moved = (1 - _AQ) * E
    E -= moved
    ta, ts = x[0], x[1] + h * moved
    return ([A[0][0] * ta + A[0][1] * ts
             + sum(b * v for b, v in zip(B[0], u)),
             A[1][0] * ta + A[1][1] * ts
             + sum(b * v for b, v in zip(B[1], u))], E)


def _update(x, P, i, z, R):
    """Scalar measurement of state i."""
    S = P[i][i] + R
    v = z - x[i]
    K = [P[0][i] / S, P[1][i] / S]
    x = [x[0] + K[0] * v, x[1] + K[1] * v]
    Pi = [P[i][0], P[i][1]]
    P = [[P[a][b] - K[a] * Pi[b] for b in range(2)] for a in range(2)]
    return x, P, v, S


def kfilter(p, steps, coarse=False, x0=None, P0=None):
    """Run the filter over steps. -> (x, P, nll, n_obs). x = [Ta, Ts, E]
    at the end of the last step (i.e. at steps[-1]['t'] + STEP); E is the
    heat still in the tube (deterministic, not in P)."""
    A, B = discretize(p)
    h = p["h"]
    Ra = R_AIR_COARSE if coarse else R_AIR
    x, P = (x0[:2], P0) if x0 else (None, None)
    E = x0[2] if x0 else 0.0
    nll = 0.0
    n = 0
    observed = any(r["F"] is not None or r["D"] is not None for r in steps)
    idle = 0
    for r in steps:
        idle = 0 if r["q"] > 0 else idle + 1
        if x is None:
            if r["T"] is None:
                E += r["q"]
                continue
            ts = r["F"] if r["F"] is not None else (r["D"] or r["T"])
            x = [r["T"], ts]
            P = [[Ra, 0.0], [0.0, 1.0]]
        obs = []
        if r["T"] is not None:
            obs.append((0, r["T"], Ra))
        if r["F"] is not None:
            obs.append((1, r["F"], R_FLOOR))
        if r["D"] is not None:
            obs.append((1, r["D"], R_DIP))
        if (not observed and r["T"] is not None
                and idle * STEP >= PSEUDO_IDLE_H * 3600):
            x, P, _, _ = _update(x, P, 1, r["T"], R_PSEUDO)
        for i, z, R in obs:
            x, P, v, S = _update(x, P, i, z, R)
            if r["away"]:
                nll += 0.5 * (v * v / S + math.log(S))
                n += 1
        if r["Tout"] is None:
            continue
        x, E = _step(A, B, x, r, h, E)
        AP = _mm(A, P)
        P = _mm(AP, [[A[0][0], A[1][0]], [A[0][1], A[1][1]]])
        P[0][0] += Q_AIR
        P[1][1] += Q_SLAB
    return (x + [E] if x else None), P, nll, n


def simulate(p, x, steps):
    """Free-run from state [Ta, Ts, E] at steps[0]['t'].
    -> [(Ta, Ts) per step]."""
    A, B = discretize(p)
    E = x[2] if len(x) > 2 else 0.0
    x = x[:2]
    out = []
    for r in steps:
        out.append((x[0], x[1]))
        if r["Tout"] is not None:
            x, E = _step(A, B, x, r, p["h"], E)
    return out


# --------------------------------------------------------------- fit ------
def _theta(p):
    return [math.log(p[k]) for k in NAMES]


def _params(th):
    return {k: math.exp(v) for k, v in zip(NAMES, th)}


def _prior_cost(th):
    c = 0.0
    for k, v in zip(NAMES, th):
        mu, sd = PRIOR[k]
        c += 0.5 * ((v - math.log(mu)) / sd) ** 2
    return c


def default_params():
    return {k: PRIOR[k][0] for k in NAMES}


def _nelder_mead(f, x0, step=0.4, iters=300, tol=1e-4):
    n = len(x0)
    pts = [list(x0)]
    for i in range(n):
        x = list(x0)
        x[i] += step
        pts.append(x)
    vals = [f(x) for x in pts]
    for _ in range(iters):
        order = sorted(range(n + 1), key=lambda i: vals[i])
        pts = [pts[i] for i in order]
        vals = [vals[i] for i in order]
        if abs(vals[-1] - vals[0]) < tol:
            break
        c = [sum(p[i] for p in pts[:-1]) / n for i in range(n)]
        xr = [c[i] + (c[i] - pts[-1][i]) for i in range(n)]
        fr = f(xr)
        if fr < vals[0]:
            xe = [c[i] + 2 * (c[i] - pts[-1][i]) for i in range(n)]
            fe = f(xe)
            pts[-1], vals[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < vals[-2]:
            pts[-1], vals[-1] = xr, fr
        else:
            xc = [c[i] + 0.5 * (pts[-1][i] - c[i]) for i in range(n)]
            fc = f(xc)
            if fc < vals[-1]:
                pts[-1], vals[-1] = xc, fc
            else:
                for j in range(1, n + 1):
                    pts[j] = [pts[0][i] + 0.5 * (pts[j][i] - pts[0][i])
                              for i in range(n)]
                    vals[j] = f(pts[j])
    i = min(range(n + 1), key=lambda i: vals[i])
    return pts[i], vals[i]


def fit(steps, coarse=False, start=None, iters=600):
    """-> (params, nll+prior, n_obs). Starts from `start` (yesterday's fit)
    and from the prior medians and keeps the better result."""
    nobs = [0]

    def f(th):
        if any(abs(v) > 12 for v in th):
            return 1e9
        p = _params(th)
        dt = STEP / 3600.0 / SUB
        if (dt * (p["kas"] + p["kao"]) > 0.9
                or dt * (p["r"] * p["kas"] + p["ksg"]) > 0.9):
            return 1e9   # keep Euler stable
        _, _, nll, n = kfilter(p, steps, coarse)
        nobs[0] = n
        return nll + _prior_cost(th)

    best = None
    for s in ([start] if start else []) + [default_params()]:
        th, cost = _nelder_mead(f, _theta(s), iters=iters)
        if best is None or cost < best[1]:
            best = (th, cost)
    f(best[0])
    return _params(best[0]), best[1], nobs[0]


def robust_low(steps, start, end):
    vals = [r["T"] for r in steps if start <= r["t"] < end
            and r["T"] is not None]
    return min(vals) if vals else None

# ---------------------------------------------------------- decision ------
def charge_rate(steps):
    """Typical heat per step (C*min) while the zone calls for a whole step:
    75th percentile of the steps that had any heat."""
    v = sorted(r["q"] * 100.0 for r in steps if r["q"] > 0)
    return v[int(0.75 * (len(v) - 1))] if len(v) >= 4 else None


def charge_profile(steps, start, end, rate, Q):
    """Copy of steps with Q (C*min) delivered from start at `rate` per step
    until done or `end`."""
    out = []
    left = Q
    for r in steps:
        r = dict(r)
        r["q"] = 0.0
        if start <= r["t"] < end and left > 0:
            d = min(rate, left)
            r["q"] = d / 100.0
            left -= d
        out.append(r)
    return out


def required_charge(p, x, future, start, end, rate, cap, target, low_start,
                    low_end, tol=10.0):
    """Smallest charge Q (C*min, delivered from `start`) that keeps the
    simulated air low over [low_start, low_end) at or above target.
    -> (Q, low_without, low_with, capped) or None."""
    def low(Q):
        sim = simulate(p, x, charge_profile(future, start, end, rate, Q))
        v = [a for (a, _), r in zip(sim, future)
             if low_start <= r["t"] < low_end]
        return min(v) if v else None

    l0 = low(0.0)
    if l0 is None:
        return None
    if l0 >= target:
        return 0.0, l0, l0, False
    lmax = low(cap)
    if lmax < target:
        return cap, l0, lmax, True
    a, b = 0.0, cap
    while b - a > tol:
        m = 0.5 * (a + b)
        if low(m) < target:
            a = m
        else:
            b = m
    return b, l0, low(b), False
