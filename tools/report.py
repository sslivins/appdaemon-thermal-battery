"""Morning report for the Thermal Battery app.

Run on the AppDaemon host:  python3 tools/report.py [--days N]
Reads thermal_battery.db and thermal_battery.log next to the app (read-only).
"""

import argparse
import datetime as dt
import json
import os
import sqlite3

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _fmt(ts, f="%H:%M"):
    return dt.datetime.fromtimestamp(ts).strftime(f) if ts else "-"


def _predictions(db, since):
    try:
        rows = db.execute(
            "SELECT cycle_id, zone, issued_ts, kind, t0, pred_low,"
            " persist_low, actual_low, valid, plausible FROM predictions"
            " WHERE cycle_id>=? ORDER BY cycle_id, zone, issued_ts",
            (since,)).fetchall()
    except sqlite3.OperationalError:
        return
    print("\n== Shadow no-heat morning-low predictions since %s" % since)
    print("   %-10s %-20s %-5s %-6s %5s %5s %5s %5s  %s" % (
        "cycle", "zone", "at", "kind", "now", "pred", "last", "act", "note"))
    err = {}
    for cid, zone, ts, kind, t0, pred, persist, act, valid, ok in rows:
        note = []
        if not ok:
            note.append("implausible")
        if act is not None and not valid:
            note.append("heated")
        print("   %-10s %-20s %-5s %-6s %5s %5s %5s %5s  %s" % (
            cid, zone, _fmt(ts), kind, _r(t0), _r(pred), _r(persist),
            _r(act), " ".join(note)))
        if valid and act is not None and persist is not None:
            e = err.setdefault((zone, kind), [])
            e.append((abs(pred - act), abs(persist - act)))
    if err:
        print("   MAE on valid (no-heat) nights: model vs persistence")
        for (zone, kind), e in sorted(err.items()):
            print("   %-20s %-6s n=%-3d model %.2f  persist %.2f" % (
                zone, kind, len(e), sum(x[0] for x in e) / len(e),
                sum(x[1] for x in e) / len(e)))


def _forecast_check(db, days):
    """9am forecast as issued vs measured outdoor (samples.outdoor)."""
    try:
        zone = db.execute("SELECT zone FROM samples WHERE outdoor IS NOT"
                          " NULL LIMIT 1").fetchone()
        snaps = [r[0] for r in db.execute(
            "SELECT DISTINCT issued_ts FROM forecast_snap ORDER BY 1")]
    except sqlite3.OperationalError:
        return
    if not zone or not snaps:
        return
    print("\n== 9am forecast vs measured outdoor (fcst - measured)")
    print("   %-10s %-5s %6s %6s %6s %8s %8s" % (
        "day", "iss", "n_hr", "bias", "MAE", "HDHfcst", "HDHmeas"))
    today = dt.date.today()
    for d in range(days, -1, -1):
        day = today - dt.timedelta(days=d)
        t9 = dt.datetime.combine(day, dt.time(9)).timestamp()
        iss = min(snaps, key=lambda s: abs(s - t9))
        if abs(iss - t9) > 3600:
            continue
        fc = dict(db.execute("SELECT hour_ts, temp FROM forecast_snap WHERE"
                             " issued_ts=?", (iss,)).fetchall())
        b = {}
        for ts, v in db.execute(
                "SELECT ts, outdoor FROM samples WHERE zone=? AND ts>=? AND"
                " ts<? AND outdoor IS NOT NULL", (zone[0], int(t9),
                                                  int(t9) + 86400)):
            b.setdefault(int(ts // 3600 * 3600), []).append(v)
        hrs = [h for h in sorted(b) if h in fc and fc[h] is not None]
        if not hrs:
            continue
        meas = {h: sum(b[h]) / len(b[h]) for h in hrs}
        err = [fc[h] - meas[h] for h in hrs]
        print("   %-10s %-5s %6d %+6.2f %6.2f %8.0f %8.0f" % (
            day, _fmt(iss), len(hrs), sum(err) / len(err),
            sum(abs(e) for e in err) / len(err),
            sum(max(0, 15 - fc[h]) for h in hrs),
            sum(max(0, 15 - meas[h]) for h in hrs)))


def _r(v):
    return "-" if v is None else "%.1f" % v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--db", default=os.path.join(APP_DIR, "thermal_battery.db"))
    ap.add_argument("--log", default=os.path.join(APP_DIR, "thermal_battery.log"))
    a = ap.parse_args()

    db = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    since = (dt.date.today() - dt.timedelta(days=a.days)).isoformat()

    def kv(key):
        r = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else None

    live = kv("live") or {}
    wok = kv("weather_ok_ts") or 0
    print("== Current cycle %s (partial=%s)" % (live.get("cycle_id"),
                                                live.get("partial")))
    print("   plan HDH=%s sun=%s kWh/m2 window=%s-%s (%s C)%s" % (
        live.get("plan_hdh"), live.get("plan_solar"), _fmt(live.get("window_start")),
        _fmt(live.get("window_end")), live.get("window_mean_c"),
        "  PLAN ERROR: %s" % live["plan_error"] if live.get("plan_error")
        else ""))
    print("   weather last OK: %s" % _fmt(wok, "%Y-%m-%d %H:%M"))
    for n, z in (live.get("zones") or {}).items():
        print("   %-20s budget=%-7s deliv=%-6.0f unpl=%-6.0f (%3.0f min) %s%s"
              % (n, z.get("budget"), z.get("delivered", 0),
                 z.get("unplanned", 0), z.get("unplanned_min", 0),
                 "CHARGING " if z.get("session") else "",
                 ("invalid: " + "; ".join(z["invalid"]))
                 if z.get("invalid") else ""))

    print("\n== Graded cycles since %s" % since)
    print("   %-10s %-20s %6s %6s %5s %5s %7s %7s %7s %5s %5s %13s  %s" % (
        "cycle", "zone", "planH", "actH", "pSun", "aSun", "budget", "deliv", "unpl",
        "low", "floor", "k", "reason"))
    for r in db.execute(
            "SELECT cycle_id, zone, plan_hdh, actual_hdh, plan_solar, actual_solar, budget, delivered,"
            " unplanned, morning_low, floor_low, k_before, k_after, reason"
            " FROM cycles WHERE cycle_id>=? ORDER BY cycle_id, rowid",
            (since,)):
        c = ["-" if v is None else v for v in r]
        print("   %-10s %-20s %6s %6s %5s %5s %7s %7s %7s %5s %5s %6s>%-6s  %s"
              % tuple(c))

    print("\n== k values (learned; zones not listed are still at k_init)")
    for z, k, upd in db.execute("SELECT zone, k, updated FROM k_values"):
        print("   %-20s %.3f  (%s)" % (z, k, upd[:16]))

    _predictions(db, since)
    _forecast_check(db, a.days)

    cutoff = int((dt.datetime.now() - dt.timedelta(days=1)).timestamp())
    print("\n== Events (last 24 h)")
    for ts, zone, ev, det in db.execute(
            "SELECT ts, zone, event, detail FROM events WHERE ts>=?"
            " ORDER BY ts", (cutoff,)):
        if ev != "grade":
            print("   %s %-20s %-14s %s" % (_fmt(ts, "%m-%d %H:%M"),
                                            zone or "-", ev, det[:110]))

    print("\n== Log problems (last 24 h)")
    hits = []
    if os.path.exists(a.log):
        t0 = (dt.datetime.now() - dt.timedelta(days=1)).strftime(
            "%Y-%m-%d %H:%M")
        with open(a.log, errors="replace") as f:
            for line in f:
                if line[:16] >= t0 and any(s in line for s in (
                        "PROBLEM", "RECOVERED", "tick failed", "FAILED")):
                    hits.append(line.rstrip()[:180])
    for h in hits[-25:]:
        print("   " + h)
    if not hits:
        print("   none")


if __name__ == "__main__":
    main()
