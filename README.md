# appdaemon-thermal-battery

AppDaemon app that uses a house's **radiant floor slabs as a thermal battery**
while the house is in **away** mode. It charges each zone once a day during the
warmest (highest-COP) afternoon hours, then lets the slab carry the house
overnight. It **learns** how much heat each zone needs from a single morning
check.

> Personal project (Black Diamond Lodge, Sun Peaks), shared as-is.

## How it works

One learned number per zone, `k`:

```
budget[zone] = k[zone] x forecast heating-degree-hours (next 24 h, base 15 C)
```

- **Heat is measured, not timed.** Delivered heat is ∫(radiant supply − zone
  return) dt in °C·min. It is counted only while that zone is calling, and
  the first `flush_min` of each call is skipped. Zone pumps are fixed speed,
  so this is proportional to energy and doesn't depend on tank or supply
  temperature.
- **Charging.** The plan picks the warmest `window_hours` block between
  `window_earliest` and `window_latest`. In that block, zones are charged in
  list order (big zones first), at most `max_concurrent` at once. A zone
  charges by raising its thermostat to `charge_setpoint`, and is put back to
  `away_setpoint` once its budget is delivered, it hits `max_run_min`, or the
  window ends.
- **Backstop.** Outside charging, thermostats sit at `away_setpoint` (12 °C).
  Any heat they add on their own is recorded as **unplanned**.
- **Grading** happens every morning at `grade_time` and covers the previous
  24 h cycle. Afternoon sun spikes are ignored on purpose.
  - Unplanned (backstop) heat means the zone was under-charged.
  - If there was no backstop heat and the overnight low (`night_start` →
    `grade_time`) was above `target_low + overshoot_deadband_c`, the zone was
    over-charged.
  - The update:
    ```
    needed = delivered + unplanned        (shrunk on overshoot)
    k     += learn_alpha * (needed / actual_HDH - k)   (±max_step_frac/day)
    ```
  - Actual HDH comes from Open-Meteo's analysis for the past hours.
  - Learning is skipped on mild days, partial cycles, home mode, or manual
    setpoint changes.
  - If a zone couldn't deliver its budget (it hit max run or the window end),
    `k` is not raised. This prevents wind-up, and the grade reports
    "capacity-limited".
- **Electric backup.** While the backup runs, charging pauses and setpoints are
  restored. Charging resumes after `backup_cooldown_min`.

`dry_run: true` (the default) never touches a setpoint. It logs what it would
charge, and it still learns `k` from the backstop-only heat, which is a good
way to bootstrap the model.

## Screaming

These conditions raise an HA persistent notification, set
`binary_sensor.thermal_battery_problem`, write ERROR logs, and call any
`notify_services`:

- weather that hasn't updated for `stale_hours`
- stale or unavailable sensors
- an unavailable thermostat
- a failed setpoint restore
- a failed plan
- a crashed tick

They auto-clear on recovery. Weather problems also set
`binary_sensor.thermal_battery_weather_problem`. The MQTT availability topic
has a last will, so the entities go `unavailable` if the app dies.

## Entities (MQTT discovery)

- `sensor.thermal_battery_status`: `dry_run` / `idle` / `charging` / `home`.
  Attributes: plan, window, last grade.
- `sensor.thermal_battery_<zone>_charge`: delivered heat today. Attributes:
  budget, unplanned, k, session state.
- `sensor.thermal_battery_<zone>_k`: the learned `k`, with long-term stats.
- `binary_sensor.thermal_battery_problem`, `binary_sensor.thermal_battery_weather_problem`

## Data

SQLite `thermal_battery.db` in the app directory has these tables:

- `samples`: 60 s air, floor, setpoint, call, supply and return per zone
- `weather_hourly`
- `cycles`: one graded row per zone per day
- `events`
- `k_values`

## Install

```sh
cd ~/docker/appdaemon-base
python3 install_app.py thermal_battery \
    --repo https://github.com/sslivins/appdaemon-thermal-battery \
    --conf ~/docker/appdaemon/conf --restart
```

Requires `requests` and `paho-mqtt` (both present in the AppDaemon image) and
`arctic_mqtt_password` in `secrets.yaml`.
