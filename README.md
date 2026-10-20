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
  - Actual HDH comes from the measured outdoor temperature: the coldest of
    `outdoor_entities`, logged every minute. If fewer than
    `min_outdoor_coverage` (90%) of the hours have readings, it falls back to
    Open-Meteo's analysis. The forecast (`weather.model`, default
    `gem_seamless`) is still what plans; the report compares the 9am forecast
    with the measured values.
  - Sun on the glass is logged too, but not used yet. It is Open-Meteo's
    `global_tilted_irradiance` on a plane facing `weather.window_azimuth`
    (compass, default 202.5° SSW) at `window_tilt` (90 = vertical). This
    accounts for sun angle, day length and cloud. Each cycle stores
    `plan_solar` and `actual_solar` (kWh/m²) for a future
    `budget = k·HDH − s·sun` fit.
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

## Shadow predictor (`tb_model.py`)

Logging only; it never changes a budget. At plan time and at
`predict.times` (12:00, 15:00, 18:00), each zone predicts its **no-heat
morning low**: the minimum of the 30-min median air temperatures in the last
`morning_hours` of the cycle. Each zone is fitted by ridge regression on
30-min steps from the last `train_days` days (pure Python, no numpy). Past
steps use the measured outdoor temperature; future steps use the forecast:

```
dT = loss·(Tout−T) + settle·(M−T) + sun·I + sun_mem·S + heat·q + heat_mem·H + d
```

- `I` is sun on the glass; `S` and `H` are slow (6 h) memories of sun and heat.
- `M` is a latent mass temperature, a 24 h average of air temperature.
  Sun lifts the air quickly, but the slab absorbs most of it. After sunset the
  air drops fast back toward `M` and then flattens. The `settle` term
  captures that drop, so it isn't read as loss to outdoors and extrapolated
  all night.

At grade time, each prediction is scored against the actual robust low and
against persistence (last night's low). A prediction counts as valid only if
the zone got no heat after it was issued. The predictor needs
`min_train_days` (3) of samples first. Coefficients are response ratios, not
physical values. Implausible fits (for example a negative loss) are logged
and flagged.

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

They auto-clear on recovery. A condition only alerts once it has lasted
`problem_confirm_s` (default 120 s) and the app has been up for
`startup_grace_s` (default 300 s), so HA restarts and brief sensor blips stay
quiet. `remote_notify` also pushes each problem and recovery to a phone through another HA instance's REST API (here the home HA's `notify.mobile_app_stefan`; its token is `mi_ha_notify_token` in `secrets.yaml`, and adding a new secret needs an AppDaemon restart). To test it, fire the HA event `thermal_battery_test_notify`. Weather problems also set
`binary_sensor.thermal_battery_weather_problem`. The MQTT availability topic
has a last will, so the entities go `unavailable` if the app dies.

### Heating notifications

With `heat_notify: true` (default), `remote_notify` also pushes when any zone
starts or stops calling for heat. The message says whether it's a planned
charge or the backstop, and gives the time, air temperature and measured
outdoor temperature; the stop message adds the run time and the air
temperature change. A change must hold for `heat_notify_debounce_s`
(default 90 s), and the state seen at startup is only a baseline, so
restarts don't send pushes. Each push is also logged as a
`heat_on` / `heat_off` event.

## Entities (MQTT discovery)

- `sensor.thermal_battery_status`: `dry_run` / `idle` / `charging` / `home`.
  Attributes: plan, window, last grade.
- `sensor.thermal_battery_<zone>_charge`: delivered heat today. Attributes:
  budget, unplanned, k, session state.
- `sensor.thermal_battery_<zone>_k`: the learned `k`, with long-term stats.
- `binary_sensor.thermal_battery_problem`, `binary_sensor.thermal_battery_weather_problem`

## Data

SQLite `thermal_battery.db` in the app directory has these tables:

- `samples`: 60 s air, floor, setpoint, call, supply and return per zone,
  plus zone blinds open fraction (`covers`), outdoor lux (`lux_entity`),
  measured outdoor temperature (`outdoor_entities`) and away flag
- `weather_hourly`: latest temperature, cloud, sun and wind per hour
  (overwritten as forecasts are replaced by analysis)
- `forecast_snap`: the next 30 h of forecast as issued at each refresh
- `predictions`: shadow predictor output and scores
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

## Morning report

```sh
python3 ~/docker/appdaemon/conf/apps/thermal_battery/tools/report.py --days 3
```

Shows the current plan and charge per zone, graded cycles (morning low, k change and reason), learned `k`, shadow predictions with MAE vs persistence, the last 24 h of events, and problems found in the log.
