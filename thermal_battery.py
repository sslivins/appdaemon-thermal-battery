"""Thermal Battery - AppDaemon app (Black Diamond Lodge / Sun Peaks).

Uses the radiant floor slabs as a thermal battery while the house is in AWAY
mode: each day it "charges" every zone with a heat budget during the warmest
(most efficient, highest-COP) part of the afternoon, and lets the slab carry
the house through the night. The zone thermostats stay at the away setpoint
(e.g. 12 C) the rest of the time and act as the fail-safe backstop.

The model is deliberately simple and *learns* one number per zone:

    budget[zone] = k[zone] * forecast heating-degree-hours (next 24 h)

Heat is measured as integral(radiant_supply - zone_return) dt [C*min] while
that zone is calling (zone pumps are fixed speed, so this is proportional to
energy). Only valid while the zone's own pump runs (shared manifold), and the
first few minutes of each call (pipe flush) are skipped.

Every morning at ``grade_time`` the past 24 h cycle is graded:

    * heat the backstop had to add on its own ("unplanned") => under-charged
    * overnight low well above target with no backstop heat => over-charged
    needed = delivered + unplanned (shrunk on overshoot)
    k_obs  = needed / actual_HDH
    k     += alpha * (k_obs - k)        (clamped per day, bounded)

Afternoon sun spikes are ignored on purpose - only the night-to-morning low and
the backstop heat matter. In ``dry_run`` the app never touches a setpoint but
still learns ``k`` from the backstop-only heat, which bootstraps the model.

Failure handling ("scream"): stale/failed weather, stale sensors, failed
setpoint restores and failed plans raise an HA persistent notification, an
MQTT ``problem`` binary_sensor and ERROR-level logs, and are cleared on
recovery.
"""

import datetime as dt
import json
import os
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass
import requests

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover
    mqtt = None


OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
SP_TOL = 0.3  # setpoint comparison tolerance (C)


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _hhmm(s):
    return dt.datetime.strptime(str(s).strip(), "%H:%M").time()


def _parse_iso(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class ThermalBattery(hass.Hass):

    # ================================================================ setup ==
    def initialize(self):
        a = self.args
        self.tz = ZoneInfo(str(a.get("timezone", "America/Vancouver")))
        self.dry_run = bool(a.get("dry_run", True))

        self.away_entity = a["away_entity"]
        self.away_state = str(a.get("away_state", "on"))
        self.backup_entity = a.get("backup_entity")
        self.supply_entity = a["radiant_supply_entity"]

        self.away_setpoint = float(a.get("away_setpoint", 12.0))
        self.charge_setpoint = float(a.get("charge_setpoint", 22.0))
        self.target_low = float(a.get("target_low", self.away_setpoint + 0.5))
        self.overshoot_deadband = float(a.get("overshoot_deadband_c", 0.5))
        self.overshoot_frac_per_c = float(a.get("overshoot_frac_per_c", 0.15))
        self.unplanned_eps = float(a.get("unplanned_eps", 20.0))
        self.unplanned_min_eps = float(a.get("unplanned_min_eps", 10.0))
        self.hdh_base = float(a.get("hdh_base_c", 15.0))

        self.grade_time = _hhmm(a.get("grade_time", "09:00"))
        self.night_start = _hhmm(a.get("night_start", "22:00"))
        self.window_earliest = _hhmm(a.get("window_earliest", "11:00"))
        self.window_latest = _hhmm(a.get("window_latest", "19:00"))
        self.window_hours = int(a.get("window_hours", 5))
        self.min_session_min = float(a.get("min_session_min", 30))
        self.max_concurrent = int(a.get("max_concurrent", 2))
        self.flush_min = float(a.get("flush_min", 8))
        self.backup_cooldown_min = float(a.get("backup_cooldown_min", 20))
        self.override_grace_s = float(a.get("override_grace_s", 120))

        self.learn_alpha = float(a.get("learn_alpha", 0.3))
        self.max_step_frac = float(a.get("max_step_frac", 0.25))
        self.min_learn_hdh = float(a.get("min_learn_hdh", 10.0))
        self.min_sample_coverage = float(a.get("min_sample_coverage", 0.5))

        self.tick_s = int(a.get("tick_s", 30))
        self.sample_s = int(a.get("sample_s", 60))
        self.sensor_stale_min = float(a.get("sensor_stale_min", 10))
        self.retention_days = int(a.get("retention_days", 120))

        w = a.get("weather", {}) or {}
        self.lat = float(w.get("latitude", 50.8846))
        self.lon = float(w.get("longitude", -119.8633))
        self.elev = w.get("elevation", 1315)
        self.weather_refresh_min = float(w.get("refresh_min", 30))
        self.weather_stale_h = float(w.get("stale_hours", 3))

        self.notify_services = list(a.get("notify_services", []) or [])
        # Push via another HA instance's REST API (e.g. the home HA that has
        # the phone's mobile_app): [{url, token, service}]
        self.remote_notify = [r for r in (a.get("remote_notify") or [])
                              if r.get("url") and r.get("token")
                              and r.get("service")]

        self.zones = []
        for i, z in enumerate(a.get("zones", [])):
            self.zones.append({
                "name": str(z["name"]),
                "climate": z["climate"],
                "return_entity": z["return_entity"],
                "floor_entities": list(z.get("floor_entities", []) or []),
                "k_init": float(z.get("k_init", 2.0)),
                "k_min": float(z.get("k_min", 0.0)),
                "k_max": float(z.get("k_max", 50.0)),
                "max_run_min": float(z.get("max_run_min", 240)),
                "max_budget": float(z.get("max_budget", 5000)),
                "priority": int(z.get("priority", i)),
            })
        self.zones.sort(key=lambda z: z["priority"])
        self.zmap = {z["name"]: z for z in self.zones}

        # --- persistence --------------------------------------------------
        self.db_path = str(a.get("db_path") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "thermal_battery.db"))
        self._db_lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._init_db()

        self.k = self._load_k()
        self.live = self._kv_get("live")
        self.weather_ok_ts = (self._kv_get("weather_ok_ts") or 0)
        self._problems = {}
        self._last_tick = None
        self._last_sample = 0
        self._backup_clear_since = None
        self._started = False

        # --- MQTT ---------------------------------------------------------
        self.mqtt_enabled = bool(a.get("mqtt_enabled", False))
        self.mqtt_host = str(a.get("mqtt_host", ""))
        self.mqtt_port = int(a.get("mqtt_port", 1883))
        self.mqtt_user = a.get("mqtt_username")
        self.mqtt_pass = a.get("mqtt_password")
        self.mqtt_prefix = str(a.get("mqtt_discovery_prefix", "homeassistant"))
        self.mqtt_base = "thermal_battery"
        self.mqtt_avail = self.mqtt_base + "/availability"
        self._mqtt = None
        if self.mqtt_enabled:
            self._mqtt_init()

        self.log("ThermalBattery start: dry_run=%s zones=%s k=%s" % (
            self.dry_run, [z["name"] for z in self.zones],
            {n: round(v, 3) for n, v in self.k.items()}))

        self._refresh_weather({})
        self._startup_cycle()
        self._recover_stuck_setpoints()
        self._started = True

        self.run_every(self._tick, "now+5", self.tick_s)
        self.run_every(self._refresh_weather, "now+%d" % int(
            self.weather_refresh_min * 60), int(self.weather_refresh_min * 60))
        self.run_daily(self._on_grade_time, self.grade_time)
        self.run_every(self._watchdog, "now+60", 300)
        self.listen_event(self._on_test_notify, "thermal_battery_test_notify")

    def terminate(self):
        try:
            if self.live:
                for name, zs in self.live["zones"].items():
                    if zs.get("session"):
                        self._end_session(name, "terminate", restore=True)
                self._save_live()
        except Exception as exc:  # noqa: BLE001
            self.log("terminate restore failed: %s" % exc, level="ERROR")
        if self._mqtt:
            try:
                self._mqtt.publish(self.mqtt_avail, "offline", qos=1, retain=True)
                self._mqtt.loop_stop()
                self._mqtt.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ================================================================== db ==
    def _init_db(self):
        with self._db_lock:
            c = self._db
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY,"
                      " value TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS k_values (zone TEXT PRIMARY"
                      " KEY, k REAL, updated TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS weather_hourly (hour_ts"
                      " INTEGER PRIMARY KEY, temp REAL, cloud REAL, sw REAL,"
                      " fetched_ts INTEGER)")
            c.execute("CREATE TABLE IF NOT EXISTS samples (ts INTEGER, zone"
                      " TEXT, air REAL, floor REAL, setpoint REAL, calling"
                      " INTEGER, supply REAL, ret REAL, session INTEGER)")
            c.execute("CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts)")
            c.execute("CREATE TABLE IF NOT EXISTS events (ts INTEGER, zone"
                      " TEXT, event TEXT, detail TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS cycles (cycle_id TEXT, zone"
                      " TEXT, plan_hdh REAL, actual_hdh REAL, budget REAL,"
                      " delivered REAL, unplanned REAL, needed REAL,"
                      " morning_low REAL, floor_low REAL, k_before REAL,"
                      " k_after REAL, learned INTEGER, reason TEXT,"
                      " detail TEXT, PRIMARY KEY(cycle_id, zone))")
            c.commit()

    def _kv_get(self, key):
        with self._db_lock:
            r = self._db.execute("SELECT value FROM kv WHERE key=?",
                                 (key,)).fetchone()
        return json.loads(r[0]) if r else None

    def _kv_set(self, key, value):
        with self._db_lock:
            self._db.execute("INSERT OR REPLACE INTO kv VALUES (?,?)",
                             (key, json.dumps(value)))
            self._db.commit()

    def _load_k(self):
        with self._db_lock:
            rows = dict(self._db.execute("SELECT zone, k FROM k_values"))
        return {z["name"]: float(rows.get(z["name"], z["k_init"]))
                for z in self.zones}

    def _save_k(self, zone, k):
        with self._db_lock:
            self._db.execute("INSERT OR REPLACE INTO k_values VALUES (?,?,?)",
                             (zone, k, dt.datetime.now(self.tz).isoformat()))
            self._db.commit()

    def _save_live(self):
        self._kv_set("live", self.live)

    def _event(self, zone, event, detail=""):
        self.log("%s %s %s" % (zone or "-", event, detail))
        with self._db_lock:
            self._db.execute("INSERT INTO events VALUES (?,?,?,?)",
                             (int(time.time()), zone, event, str(detail)))
            self._db.commit()

    # ============================================================= helpers ==
    def _now(self):
        return dt.datetime.now(self.tz)

    def _cycle_bounds(self, now=None):
        now = now or self._now()
        start = now.replace(hour=self.grade_time.hour,
                            minute=self.grade_time.minute,
                            second=0, microsecond=0)
        if now < start:
            start -= dt.timedelta(days=1)
        return start, start + dt.timedelta(days=1)

    def _at(self, day_dt, t):
        return day_dt.replace(hour=t.hour, minute=t.minute, second=0,
                              microsecond=0)

    def _is_away(self):
        return str(self.get_state(self.away_entity)) == self.away_state

    def _backup_on(self):
        if not self.backup_entity:
            return False
        return str(self.get_state(self.backup_entity)) == "on"

    def _read(self, entity):
        """-> (value, age_s) ; value None if unavailable."""
        st = self.get_state(entity, attribute="all") or {}
        val = _num(st.get("state"))
        ts = _parse_iso(st.get("last_reported")) or _parse_iso(
            st.get("last_updated"))
        age = (time.time() - ts) if ts else None
        return val, age

    def _climate(self, z):
        st = self.get_state(z["climate"], attribute="all") or {}
        at = st.get("attributes", {}) or {}
        return {
            "available": st.get("state") not in (None, "unavailable",
                                                 "unknown"),
            "action": at.get("hvac_action"),
            "setpoint": _num(at.get("temperature")),
            "air": _num(at.get("current_temperature")),
        }

    def _floor(self, z):
        vals = [_num(self.get_state(e)) for e in z["floor_entities"]]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    def _apply(self, z, temp, why):
        if self.dry_run:
            self.log("[dry_run] would set %s -> %.1fC (%s)" % (
                z["climate"], temp, why))
            return
        self.call_service("climate/set_temperature", entity_id=z["climate"],
                          temperature=temp)
        self._event(z["name"], "set_temperature", "%.1f (%s)" % (temp, why))

    # ============================================================= weather ==
    def _refresh_weather(self, kwargs):
        params = {
            "latitude": self.lat, "longitude": self.lon,
            "elevation": self.elev,
            "hourly": "temperature_2m,cloud_cover,shortwave_radiation",
            "past_days": 3, "forecast_days": 3,
            "timezone": "UTC", "timeformat": "unixtime",
        }
        try:
            r = requests.get(OPEN_METEO_URL, params=params, timeout=25)
            r.raise_for_status()
            h = r.json()["hourly"]
            ts, temp = h["time"], h["temperature_2m"]
            cloud = h.get("cloud_cover") or [None] * len(ts)
            sw = h.get("shortwave_radiation") or [None] * len(ts)
            if not ts or len(temp) != len(ts):
                raise ValueError("malformed hourly payload")
            now = time.time()
            ahead = [t for t, v in zip(ts, temp)
                     if now <= t < now + 24 * 3600 and v is not None]
            if len(ahead) < 22:
                raise ValueError("only %d valid forecast hours ahead"
                                 % len(ahead))
            fetched = int(now)
            rows = [(int(t), _num(v), _num(c), _num(s), fetched)
                    for t, v, c, s in zip(ts, temp, cloud, sw)
                    if v is not None]
            with self._db_lock:
                self._db.executemany(
                    "INSERT OR REPLACE INTO weather_hourly VALUES (?,?,?,?,?)",
                    rows)
                self._db.commit()
            self.weather_ok_ts = fetched
            self._kv_set("weather_ok_ts", fetched)
            self.log("weather: %d hourly rows (next: %.1fC)" % (
                len(rows), _num(temp[ts.index(ahead[0])]) or 0), level="DEBUG")
        except Exception as exc:  # noqa: BLE001
            self.log("weather fetch FAILED: %s" % exc, level="WARNING")
        self._check_weather()
        # A plan may have been blocked waiting for weather.
        if self._started and self.live and self.live.get("plan_error"):
            self._make_plan(retry=True)

    def _check_weather(self):
        age_h = (time.time() - self.weather_ok_ts) / 3600.0
        if age_h > self.weather_stale_h:
            last = (dt.datetime.fromtimestamp(self.weather_ok_ts, self.tz)
                    .strftime("%Y-%m-%d %H:%M") if self.weather_ok_ts
                    else "never")
            self._set_problem(
                "weather", True,
                "Open-Meteo weather has not updated for %.1f h (last good: "
                "%s). Thermal Battery cannot plan or learn; zones fall back "
                "to their %.0fC thermostats." % (age_h, last,
                                                 self.away_setpoint))
        else:
            self._set_problem("weather", False)

    def _hourly(self, start_dt, end_dt):
        with self._db_lock:
            return self._db.execute(
                "SELECT hour_ts, temp, cloud, sw FROM weather_hourly WHERE"
                " hour_ts>=? AND hour_ts<? ORDER BY hour_ts",
                (int(start_dt.timestamp()), int(end_dt.timestamp()))).fetchall()

    def _hdh(self, start_dt, end_dt):
        rows = self._hourly(start_dt, end_dt)
        expected = int(round((end_dt - start_dt).total_seconds() / 3600))
        if len(rows) < expected - 2:
            return None, rows
        hdh = sum(max(0.0, self.hdh_base - r[1]) for r in rows) * (
            expected / float(len(rows)))
        return hdh, rows

    # =============================================================== cycle ==
    def _new_live(self, cycle_start, partial):
        return {
            "cycle_id": cycle_start.strftime("%Y-%m-%d"),
            "cycle_start": cycle_start.timestamp(),
            "partial": partial,
            "plan_hdh": None, "plan_error": None,
            "window_start": None, "window_end": None,
            "zones": {z["name"]: {
                "budget": None, "k_used": None,
                "delivered": 0.0, "unplanned": 0.0, "unplanned_min": 0.0,
                "run_min": 0.0, "session": None, "done": False,
                "call_start": None, "invalid": [], "restore": None,
                "stop_reason": None,
            } for z in self.zones},
        }

    def _startup_cycle(self):
        cs, _ = self._cycle_bounds()
        cid = cs.strftime("%Y-%m-%d")
        if self.live and self.live.get("cycle_id") == cid:
            # Same cycle - make sure new zones exist.
            for z in self.zones:
                self.live["zones"].setdefault(
                    z["name"], self._new_live(cs, True)["zones"][z["name"]])
            if self.live.get("plan_hdh") is None:
                self._make_plan(retry=True)
            self._event(None, "resume_cycle", cid)
            return
        if self.live:
            self._event(None, "late_grade", self.live.get("cycle_id"))
            self._grade()
        self.live = self._new_live(cs, partial=True)
        self._make_plan()

    def _on_grade_time(self, kwargs):
        cs, _ = self._cycle_bounds()
        if self.live and self.live.get("cycle_id") == cs.strftime("%Y-%m-%d"):
            return  # already rolled (tick and run_daily can race)
        self._refresh_weather({})
        self._grade()
        self.live = self._new_live(cs, partial=False)
        self._make_plan()

    def _make_plan(self, retry=False):
        live = self.live
        cs = dt.datetime.fromtimestamp(live["cycle_start"], self.tz)
        ce = cs + dt.timedelta(days=1)
        hdh, rows = self._hdh(cs, ce)
        if hdh is None:
            live["plan_error"] = "no forecast for cycle"
            self._save_live()
            self._set_problem("plan", True, "Thermal Battery could not plan "
                              "cycle %s: no weather forecast. No charging "
                              "today; thermostats are the backstop."
                              % live["cycle_id"])
            return
        # Warmest contiguous block inside [earliest, latest].
        temps = {r[0]: r[1] for r in rows}
        best = None
        day = cs
        t = self._at(day, self.window_earliest)
        last_start = self._at(day, self.window_latest) - dt.timedelta(
            hours=self.window_hours)
        while t <= last_start:
            hs = [temps.get(int((t + dt.timedelta(hours=i)).timestamp()))
                  for i in range(self.window_hours)]
            hs = [x for x in hs if x is not None]
            if hs:
                m = sum(hs) / len(hs)
                if best is None or m > best[0]:
                    best = (m, t)
            t += dt.timedelta(hours=1)
        ws = best[1] if best else self._at(day, self.window_earliest)
        we = ws + dt.timedelta(hours=self.window_hours)
        live["plan_hdh"] = round(hdh, 1)
        live["plan_error"] = None
        live["window_start"] = ws.timestamp()
        live["window_end"] = we.timestamp()
        live["window_mean_c"] = round(best[0], 1) if best else None
        for z in self.zones:
            zs = live["zones"][z["name"]]
            k = self.k[z["name"]]
            zs["k_used"] = round(k, 4)
            zs["budget"] = round(min(k * hdh, z["max_budget"]), 1)
        self._save_live()
        self._set_problem("plan", False)
        self._event(None, "plan", "%s HDH=%.1f window=%s-%s (%.1fC) budgets=%s%s"
                    % (live["cycle_id"], hdh, ws.strftime("%H:%M"),
                       we.strftime("%H:%M"), live.get("window_mean_c") or 0,
                       {n: s["budget"] for n, s in live["zones"].items()},
                       " (retry)" if retry else ""))

    # ============================================================== grade ==
    def _grade(self):
        live = self.live
        if not live:
            return
        cs = dt.datetime.fromtimestamp(live["cycle_start"], self.tz)
        ce = cs + dt.timedelta(days=1)
        # End any session still open (shouldn't happen - window is earlier).
        for name, zs in live["zones"].items():
            if zs.get("session"):
                self._end_session(name, "grade", restore=True)
        actual_hdh, _ = self._hdh(cs, ce)
        night_start = self._at(cs, self.night_start)
        summary = []
        for z in self.zones:
            name = z["name"]
            zs = live["zones"].get(name)
            if not zs:
                continue
            low, floor_low, coverage = self._night_low(name, night_start, ce)
            delivered, unplanned = zs["delivered"], zs["unplanned"]
            unplanned_min = zs.get("unplanned_min", 0.0)
            needed = delivered + unplanned
            overshoot = False
            if (unplanned < self.unplanned_eps
                    and unplanned_min < self.unplanned_min_eps
                    and low is not None
                    and low > self.target_low + self.overshoot_deadband):
                overshoot = True
                needed *= max(0.5, 1.0 - self.overshoot_frac_per_c *
                              (low - self.target_low))
            k_before = self.k[name]
            k_after = k_before
            reasons = list(zs.get("invalid", []))
            if live.get("partial"):
                reasons.append("partial cycle")
            if actual_hdh is None:
                reasons.append("no weather actuals")
            elif actual_hdh < self.min_learn_hdh:
                reasons.append("HDH %.1f < %.1f" % (actual_hdh,
                                                    self.min_learn_hdh))
            if coverage < self.min_sample_coverage:
                reasons.append("night samples %.0f%%" % (coverage * 100))
            if overshoot and delivered < self.unplanned_eps:
                # We added nothing and the house stayed warm: it is coasting
                # on stored/solar heat. No information about k - don't learn.
                reasons.append("coasting (no heat added, low %.1f)" % low)
            learned = not reasons
            capacity_limited = zs.get("stop_reason") in ("max run",
                                                          "window end")
            if learned:
                k_obs = needed / actual_hdh
                if capacity_limited and k_obs > k_before:
                    # Couldn't deliver the budget anyway - raising k would
                    # just wind up. Hold, and report it.
                    k_obs = k_before
                    reasons.append("capacity-limited")
                k_new = k_before + self.learn_alpha * (k_obs - k_before)
                if k_before > 0:
                    lo = k_before * (1 - self.max_step_frac)
                    hi = k_before * (1 + self.max_step_frac)
                    k_new = min(max(k_new, lo), hi)
                k_after = min(max(k_new, z["k_min"]), z["k_max"])
                self.k[name] = k_after
                self._save_k(name, k_after)
            reason = "; ".join(reasons) if reasons else (
                "overshoot" if overshoot else "ok")
            detail = {"overshoot": overshoot, "stop_reason": zs.get(
                "stop_reason"), "run_min": round(zs.get("run_min", 0), 1),
                "unplanned_min": round(unplanned_min, 1),
                "dry_run": self.dry_run, "coverage": round(coverage, 2)}
            with self._db_lock:
                self._db.execute(
                    "INSERT OR REPLACE INTO cycles VALUES"
                    " (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (live["cycle_id"], name, live.get("plan_hdh"),
                     None if actual_hdh is None else round(actual_hdh, 1),
                     zs.get("budget"), round(delivered, 1),
                     round(unplanned, 1), round(needed, 1), low, floor_low,
                     round(k_before, 4), round(k_after, 4), int(learned),
                     reason, json.dumps(detail)))
                self._db.commit()
            summary.append("%s: deliv=%.0f unpl=%.0f (%.0fmin) low=%s k %.3f->%.3f (%s)"
                           % (name, delivered, unplanned, unplanned_min, low,
                              k_before, k_after, reason))
            zs["last_grade"] = {"low": low, "floor_low": floor_low,
                                "k_before": round(k_before, 4),
                                "k_after": round(k_after, 4),
                                "reason": reason}
        self._kv_set("last_grade", {"cycle_id": live["cycle_id"],
                                    "actual_hdh": actual_hdh,
                                    "zones": {n: s.get("last_grade") for n, s
                                              in live["zones"].items()}})
        self._event(None, "grade", "%s actualHDH=%s | %s" % (
            live["cycle_id"], None if actual_hdh is None else
            round(actual_hdh, 1), " | ".join(summary)))
        self._prune()

    def _night_low(self, zone, start, end):
        with self._db_lock:
            rows = self._db.execute(
                "SELECT air, floor FROM samples WHERE zone=? AND ts>=? AND"
                " ts<?", (zone, int(start.timestamp()),
                          int(end.timestamp()))).fetchall()
        expected = max(1.0, (end - start).total_seconds() / self.sample_s)
        airs = [r[0] for r in rows if r[0] is not None]
        floors = [r[1] for r in rows if r[1] is not None]
        return (min(airs) if airs else None,
                min(floors) if floors else None,
                len(airs) / expected)

    def _prune(self):
        cutoff = int(time.time() - self.retention_days * 86400)
        with self._db_lock:
            self._db.execute("DELETE FROM samples WHERE ts<?", (cutoff,))
            self._db.execute("DELETE FROM weather_hourly WHERE hour_ts<?",
                             (cutoff,))
            self._db.commit()

    # ================================================================ tick ==
    def _tick(self, kwargs):
        try:
            self._tick_inner()
        except Exception as exc:  # noqa: BLE001
            self.log("tick failed: %s" % exc, level="ERROR")
            self._set_problem("tick", True, "Thermal Battery tick crashed: %s"
                              % exc)
            return
        self._set_problem("tick", False)

    def _tick_inner(self):
        live = self.live
        now = time.time()
        dt_min = 0.0
        if self._last_tick is not None:
            gap = now - self._last_tick
            if gap <= 3 * self.tick_s:
                dt_min = gap / 60.0
        self._last_tick = now

        cs, _ = self._cycle_bounds()
        if cs.strftime("%Y-%m-%d") != live["cycle_id"]:
            # Missed the daily callback (e.g. clock jump) - roll now.
            self._on_grade_time({})
            live = self.live

        away = self._is_away()
        backup = self._backup_on()
        if backup:
            self._backup_clear_since = None
        elif self._backup_clear_since is None:
            self._backup_clear_since = now

        sup, sup_age = self._read(self.supply_entity)
        any_flow_long = False
        do_sample = now - self._last_sample >= self.sample_s
        if do_sample:
            self._last_sample = now
        samples = []

        for z in self.zones:
            name = z["name"]
            zs = live["zones"][name]
            c = self._climate(z)
            ret, ret_age = self._read(z["return_entity"])
            if not c["available"]:
                self._set_problem("climate:" + name, True,
                                  "%s is unavailable." % z["climate"])
            else:
                self._set_problem("climate:" + name, False)
            calling = c["action"] == "heating"
            if calling and zs["call_start"] is None:
                zs["call_start"] = now
            elif not calling:
                zs["call_start"] = None
            flow_long = bool(calling and now - zs["call_start"]
                             >= self.sensor_stale_min * 60)
            any_flow_long = any_flow_long or flow_long
            self._stale_check(z["return_entity"], ret, ret_age, flow_long)

            # --- heat accounting ---------------------------------------
            if (calling and dt_min > 0 and sup is not None and ret is not None
                    and now - zs["call_start"] >= self.flush_min * 60):
                q = max(0.0, sup - ret) * dt_min
                if zs["session"]:
                    zs["delivered"] += q
                else:
                    zs["unplanned"] += q
            if calling and dt_min > 0 and not zs["session"]:
                zs["unplanned_min"] = zs.get("unplanned_min", 0.0) + dt_min
            if zs["session"]:
                zs["run_min"] += dt_min

            # --- validity for learning ----------------------------------
            if not away:
                self._invalidate(zs, "home mode")
            elif (not zs["session"] and not zs["restore"]
                  and c["setpoint"] is not None
                  and abs(c["setpoint"] - self.away_setpoint) > SP_TOL):
                self._invalidate(zs, "manual setpoint %.1f" % c["setpoint"])

            # --- session supervision ------------------------------------
            if zs["session"]:
                self._supervise(z, zs, c, away, backup, now)
            elif zs["restore"]:
                self._verify_restore(z, zs, c, now)

            if do_sample:
                samples.append((int(now), name, c["air"], self._floor(z),
                                c["setpoint"], int(calling), sup, ret,
                                int(bool(zs["session"]))))

        self._maybe_start_sessions(away, backup, now)
        self._stale_check(self.supply_entity, sup, sup_age, any_flow_long)

        if samples:
            with self._db_lock:
                self._db.executemany(
                    "INSERT INTO samples VALUES (?,?,?,?,?,?,?,?,?)", samples)
                self._db.commit()
        self._save_live()
        if do_sample:
            self._publish_all()

    def _invalidate(self, zs, reason):
        if reason not in zs["invalid"]:
            zs["invalid"].append(reason)

    def _stale_check(self, entity, val, age, flowing=True):
        """Unavailable => problem. Unchanged for too long => problem, but only
        while water is moving past the probe (HA drops identical MQTT
        payloads, so a stable reading in a still pipe is normal)."""
        frozen = flowing and age is not None and age > self.sensor_stale_min * 60
        bad = val is None or frozen
        self._set_problem(
            "sensor:" + entity, bad,
            "%s is %s." % (entity, "unavailable" if val is None else
                           "frozen (unchanged %.0f min while flowing)"
                           % ((age or 0) / 60)))

    # ============================================================ sessions ==
    def _in_window(self, now, min_left_min=0):
        ws, we = self.live.get("window_start"), self.live.get("window_end")
        if ws is None:
            return False
        return ws <= now < we - min_left_min * 60

    def _maybe_start_sessions(self, away, backup, now):
        live = self.live
        if (self.dry_run or not away or backup or live.get("plan_hdh") is None
                or not self._in_window(now, self.min_session_min)):
            if self.dry_run and self._in_window(now) and not live.get(
                    "dry_logged"):
                live["dry_logged"] = True
                self.log("[dry_run] window open: would charge %s" % {
                    n: "%.0f (have %.0f)" % (s["budget"] or 0,
                                             s["delivered"] + s["unplanned"])
                    for n, s in live["zones"].items()})
            return
        if (self._backup_clear_since is None or
                now - self._backup_clear_since < self.backup_cooldown_min * 60):
            return
        active = sum(1 for s in live["zones"].values() if s["session"])
        for z in self.zones:
            if active >= self.max_concurrent:
                break
            zs = live["zones"][z["name"]]
            if zs["session"] or zs["done"] or zs["restore"]:
                continue
            remaining = (zs["budget"] or 0) - zs["delivered"] - zs["unplanned"]
            if remaining <= 0:
                zs["done"] = True
                zs["stop_reason"] = zs["stop_reason"] or "no need"
                continue
            if zs["run_min"] >= z["max_run_min"]:
                continue
            c = self._climate(z)
            if (c["setpoint"] is None
                    or abs(c["setpoint"] - self.away_setpoint) > SP_TOL):
                continue  # someone else is driving this zone
            zs["session"] = {"start": now, "set_at": now}
            self._apply(z, self.charge_setpoint, "charge start, need %.0f"
                        % remaining)
            self._event(z["name"], "session_start", "budget=%.0f remaining=%.0f"
                        % (zs["budget"] or 0, remaining))
            active += 1

    def _supervise(self, z, zs, c, away, backup, now):
        name = z["name"]
        if not away:
            self._end_session(name, "home mode", restore=True)
            return
        if backup:
            self._end_session(name, "backup running", restore=True,
                              done=False)
            return
        if (c["setpoint"] is not None
                and now - zs["session"]["set_at"] > self.override_grace_s
                and abs(c["setpoint"] - self.charge_setpoint) > SP_TOL):
            self._invalidate(zs, "override during charge %.1f" % c["setpoint"])
            self._end_session(name, "manual override", restore=False)
            return
        if zs["delivered"] + zs["unplanned"] >= (zs["budget"] or 0):
            self._end_session(name, "budget reached", restore=True)
        elif zs["run_min"] >= z["max_run_min"]:
            self._end_session(name, "max run", restore=True)
        elif now >= (self.live.get("window_end") or 0):
            self._end_session(name, "window end", restore=True)

    def _end_session(self, name, reason, restore=True, done=True):
        z = self.zmap[name]
        zs = self.live["zones"][name]
        if not zs.get("session"):
            return
        zs["session"] = None
        zs["done"] = done
        zs["stop_reason"] = reason
        if restore:
            self._apply(z, self.away_setpoint, "charge end: " + reason)
            zs["restore"] = {"at": time.time(), "tries": 1}
        self._event(name, "session_end", "%s delivered=%.0f/%s run=%.0fmin"
                    % (reason, zs["delivered"], zs.get("budget"),
                       zs["run_min"]))

    def _verify_restore(self, z, zs, c, now):
        r = zs["restore"]
        if self.dry_run or c["setpoint"] is None:
            zs["restore"] = None
            return
        if abs(c["setpoint"] - self.away_setpoint) <= SP_TOL:
            zs["restore"] = None
            self._set_problem("restore:" + z["name"], False)
            return
        if abs(c["setpoint"] - self.charge_setpoint) > SP_TOL:
            zs["restore"] = None  # someone changed it to something else
            return
        if now - r["at"] < 60:
            return
        if r["tries"] >= 3:
            self._set_problem("restore:" + z["name"], True,
                              "Could not restore %s to %.1fC after %d tries "
                              "(still %.1fC)." % (z["climate"],
                                                  self.away_setpoint,
                                                  r["tries"], c["setpoint"]))
        self._apply(z, self.away_setpoint, "restore retry")
        r["at"] = now
        r["tries"] += 1

    def _recover_stuck_setpoints(self):
        """After a crash, put zones we were charging back to baseline."""
        if self.dry_run or not self._is_away():
            return
        for z in self.zones:
            zs = self.live["zones"][z["name"]]
            if zs.get("session"):
                zs["session"] = None
                zs["done"] = False
                c = self._climate(z)
                if (c["setpoint"] is not None and
                        abs(c["setpoint"] - self.charge_setpoint) <= SP_TOL):
                    self._apply(z, self.away_setpoint, "startup recovery")
                    zs["restore"] = {"at": time.time(), "tries": 1}
                self._event(z["name"], "startup_recovery", "")
        self._save_live()

    def _watchdog(self, kwargs):
        self._check_weather()
        if self._problems:
            self.log("ACTIVE PROBLEMS: %s" % "; ".join(
                self._problems.values()), level="ERROR")

    # ============================================================ problems ==
    def _set_problem(self, key, active, message=""):
        nid = "thermal_battery_" + key.replace(":", "_").replace(".", "_")
        if active:
            if key in self._problems:
                self._problems[key] = message
                return
            self._problems[key] = message
            self.log("PROBLEM [%s]: %s" % (key, message), level="ERROR")
            try:
                self.call_service("persistent_notification/create",
                                  title="Thermal Battery problem",
                                  message=message, notification_id=nid)
            except Exception as exc:  # noqa: BLE001
                self.log("notify failed: %s" % exc, level="ERROR")
            for svc in self.notify_services:
                try:
                    self.call_service(svc, title="Thermal Battery problem",
                                      message=message)
                except Exception as exc:  # noqa: BLE001
                    self.log("notify %s failed: %s" % (svc, exc),
                             level="ERROR")
            self._push("Thermal Battery problem", message)
            self._publish_status()
        elif key in self._problems:
            del self._problems[key]
            self.log("RECOVERED [%s]" % key, level="WARNING")
            self._push("Thermal Battery recovered", "Cleared: %s" % key)
            try:
                self.call_service("persistent_notification/dismiss",
                                  notification_id=nid)
            except Exception:  # noqa: BLE001
                pass
            self._publish_status()

    def _push(self, title, message):
        for r in self.remote_notify:
            svc = str(r["service"]).replace("notify.", "").replace(
                "notify/", "")
            try:
                resp = requests.post(
                    "%s/api/services/notify/%s" % (
                        str(r["url"]).rstrip("/"), svc),
                    headers={"Authorization": "Bearer %s" % r["token"]},
                    json={"title": title, "message": message}, timeout=10)
                resp.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                self.log("remote push %s via %s failed: %s" % (
                    svc, r["url"], exc), level="ERROR")

    def _on_test_notify(self, event, data, kwargs):
        self.log("test notify requested")
        self._push("Thermal Battery test",
                   (data or {}).get("message") or
                   "Test alert from Sun Peaks (appdaemon.bdl).")

    # ================================================================ MQTT ==
    def _mqtt_init(self):
        if mqtt is None or not self.mqtt_host:
            self.log("MQTT unavailable (paho missing or no host)",
                     level="ERROR")
            self.mqtt_enabled = False
            return
        try:
            try:
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1,
                                     client_id="thermal-battery")
            except AttributeError:  # paho < 2.0
                client = mqtt.Client(client_id="thermal-battery")
            if self.mqtt_user:
                client.username_pw_set(self.mqtt_user, self.mqtt_pass)
            client.will_set(self.mqtt_avail, "offline", qos=1, retain=True)
            client.on_connect = self._mqtt_on_connect
            self._mqtt = client
            client.connect_async(self.mqtt_host, self.mqtt_port, keepalive=60)
            client.loop_start()
        except Exception as exc:  # noqa: BLE001
            self.log("MQTT init failed: %s" % exc, level="ERROR")
            self.mqtt_enabled = False

    def _disc(self, client, component, obj, cfg):
        dev = {"identifiers": ["thermal_battery"], "name": "Thermal Battery",
               "manufacturer": "AppDaemon", "model": "learning slab charger"}
        cfg.update({"availability_topic": self.mqtt_avail, "device": dev,
                    "unique_id": "thermal_battery_" + obj})
        client.publish("%s/%s/thermal_battery/%s/config" % (
            self.mqtt_prefix, component, obj), json.dumps(cfg), qos=1,
            retain=True)

    def _mqtt_on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            self.log("MQTT connect failed rc=%s" % rc, level="ERROR")
            return
        b = self.mqtt_base
        self._disc(client, "binary_sensor", "problem", {
            "name": "Problem", "device_class": "problem",
            "state_topic": b + "/problem/state",
            "json_attributes_topic": b + "/problem/attr"})
        self._disc(client, "binary_sensor", "weather_problem", {
            "name": "Weather Problem", "device_class": "problem",
            "state_topic": b + "/weather_problem/state"})
        self._disc(client, "sensor", "status", {
            "name": "Status", "icon": "mdi:home-battery",
            "state_topic": b + "/status/state",
            "json_attributes_topic": b + "/status/attr"})
        for z in self.zones:
            n = z["name"]
            pretty = n.replace("_", " ").title()
            self._disc(client, "sensor", n + "_charge", {
                "name": pretty + " Charge", "icon": "mdi:heating-coil",
                "unit_of_measurement": "C*min", "state_class": "measurement",
                "state_topic": "%s/%s/state" % (b, n),
                "value_template": "{{ value_json.delivered }}",
                "json_attributes_topic": "%s/%s/state" % (b, n)})
            self._disc(client, "sensor", n + "_k", {
                "name": pretty + " k", "icon": "mdi:school",
                "state_class": "measurement",
                "state_topic": "%s/%s/state" % (b, n),
                "value_template": "{{ value_json.k }}"})
        client.publish(self.mqtt_avail, "online", qos=1, retain=True)
        self.log("MQTT connected; discovery published")
        self._publish_all()

    def _pub(self, topic, payload):
        if not (self.mqtt_enabled and self._mqtt):
            return
        try:
            if not isinstance(payload, str):
                payload = json.dumps(payload)
            self._mqtt.publish(topic, payload, qos=0, retain=True)
        except Exception as exc:  # noqa: BLE001
            self.log("MQTT publish failed: %s" % exc, level="WARNING")

    def _publish_status(self):
        b = self.mqtt_base
        self._pub(b + "/problem/state", "ON" if self._problems else "OFF")
        self._pub(b + "/problem/attr", {"problems": list(
            self._problems.values())})
        self._pub(b + "/weather_problem/state",
                  "ON" if "weather" in self._problems else "OFF")

    def _publish_all(self):
        if not (self.mqtt_enabled and self._mqtt) or not self.live:
            return
        live = self.live
        b = self.mqtt_base
        charging = [n for n, s in live["zones"].items() if s["session"]]
        if self.dry_run:
            state = "dry_run"
        elif not self._is_away():
            state = "home"
        elif charging:
            state = "charging"
        else:
            state = "idle"
        fmt = (lambda ts: dt.datetime.fromtimestamp(ts, self.tz).isoformat()
               if ts else None)
        self._pub(b + "/status/state", state)
        self._pub(b + "/status/attr", {
            "cycle_id": live["cycle_id"], "partial": live.get("partial"),
            "plan_hdh": live.get("plan_hdh"),
            "plan_error": live.get("plan_error"),
            "window_start": fmt(live.get("window_start")),
            "window_end": fmt(live.get("window_end")),
            "window_mean_c": live.get("window_mean_c"),
            "charging": charging, "dry_run": self.dry_run,
            "weather_last_ok": fmt(self.weather_ok_ts),
        })
        lg = self._kv_get("last_grade") or {}
        lg_zones = lg.get("zones") or {}
        for z in self.zones:
            s = live["zones"][z["name"]]
            self._pub("%s/%s/state" % (b, z["name"]), {
                "delivered": round(s["delivered"], 1),
                "unplanned": round(s["unplanned"], 1),
                "unplanned_min": round(s.get("unplanned_min", 0.0), 1),
                "budget": s["budget"], "k": round(self.k[z["name"]], 4),
                "charging": bool(s["session"]), "done": s["done"],
                "run_min": round(s["run_min"], 1),
                "stop_reason": s["stop_reason"], "invalid": s["invalid"],
                "last_grade": lg_zones.get(z["name"]),
                "last_grade_cycle": lg.get("cycle_id"),
            })
        self._publish_status()
