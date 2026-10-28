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
  - **Morning carry.** A backstop call that is running at `grade_time`, or
    starts within `carry_grace_min` (30) after it, is covering the night that
    just ended. The new cycle starts on time (plan and prediction at
    `grade_time`), but the old cycle's grade is held (`grade_held`) and that
    call's heat is booked to the old cycle until it ends. Its night low also
    extends to the end of the call. The grade then runs (`grade_release`) and
    today's budgets are re-sized with the new `k` (`rebudget`) for zones that
    haven't charged yet. The grade is forced after `carry_max_min` (240).
- **Electric backup.** While the backup runs, charging pauses and setpoints are
  restored. Charging resumes after `backup_cooldown_min`.

`dry_run: true` (the default) never touches a setpoint. It logs what it would
charge, and it still learns `k` from the backstop-only heat, which is a good
way to bootstrap the model.

`live_zones` lists zones that charge for real even while `dry_run` is on,
for a staged rollout (one zone at a time). Everything else stays in shadow.

### Probe runs

With `probe.enabled`, every zone (dry_run zones too) gets one short call
(`probe.minutes`, default 10; per-zone `probe_min`) in the `probe.lead_min`
before the charging window. The first water back sat in the loop, so the
return dip reads the slab temperature in zones without floor probes, and
the slab model gets a fresh reading before the window-open decision. The
probe must outlast the loop transit (basement master ~9.5 min). Zones that
called within `recent_call_min` are skipped (that call already read the
slab). Probe heat is kept separate (`probe_q`): it is neither delivered nor
unplanned, so it doesn't feed `k` learning, and probes don't count toward
`max_run_min`. Events: `probe_start` / `probe_end` / `probe_skip`.

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

The logged prediction (`pred_low`) can blend in last night's low:
`blend` × the model plus the rest × last night's low. The default is now
1.0 (model only). Last night's low ignores the weather, so it lags whenever a
night is colder or warmer than the one before, and it only scored well in the
whole-°F zones because their readings barely move. The raw model output is
kept in `model_low`.

## Slab model (`tb_slab.py`)

A second shadow predictor (`predict.slab: true`). The slab's stored heat is
the biggest unknown when predicting a zone, so this model tracks it
explicitly. It has two nodes, air `Ta` and slab `Ts`, plus a short "tube"
store that delays heat input by about 1 h (floor sensors peak about an hour
after a call ends). A Kalman filter estimates the slab temperature from:

- air temperature, every 30 min
- floor surface sensors (`floor_entities`), where the zone has them
- the **return-water dip**: in the first minutes of a call, the water that
  sat in the loop comes back at slab temperature before the hot front gets
  round. It is used only if the loop was idle for at least 60 min. It
  matched the floor sensors to within about 0.1–0.35 °C.

Heat input is the integral of (supply − return) while calling (pumps are
fixed speed, so this is proportional to energy). Parameters are fitted per
zone by maximum likelihood with log-normal priors. The fit runs once a day in
a background thread after the 09:00 plan (and at startup if older than 20 h);
it is warm-started and cached in `kv.slab_params`. Each predict time filters
the history and free-runs the forecast with no heat. The predictions table
gets `slab_low` (the predicted low) and `slab_t` (the estimated slab
temperature at issue).

Each heat call is logged in `loop_calls` (and as a `loop_call` event) with:

- the dip temperature
- the time the dip occurred
- the **loop transit time**: call start to the steepest rise of the return,
  when the hot front reaches the return probe

Early transits: main floor 4–5 min, master bedroom 6–6.5 min, basement
master 9.5 min (samples are 1–1.5 min apart).

Set `air_coarse: true` for zones whose thermostat reports whole °F. A zone
with no floor sensor and no heat calls has nothing that observes its slab
(basement bunk so far), so its slab estimate is weakly held near air
temperature, and its old-model prediction is likely the better one.

Backtest, Oct 13–23, hindcast from 18:00 with actual weather (MAE of the
morning low, °C):

| Zone | Old | Slab |
|---|---|---|
| Main floor | 0.53 | 0.20 |
| Master bedroom | 0.92 | 0.37 |
| Basement master | 0.49 | 0.26 |
| Ski room | 0.29 | 0.29 |
| Basement bunk | 0.16 | 0.23 |

With forecasts as issued, forecast error dominates and the two models are
close.

### Shadow charge decision

The 09:00 budget (`k` × HDH) is set hours before charging starts, and
anything in between (a backstop call, a sunny morning, a new forecast) only
reaches it through the delivered/unplanned accounting. So when the charge
window opens, the slab model makes its own decision (logged only):

- it filters all data up to now, so heat from earlier in the day is simply
  part of the slab state
- it simulates charging from now at the zone's typical rate (75th
  percentile of past heat per 30 min), with the latest forecast
- it finds the smallest charge (C·min, same units as the budget) that keeps
  the night's low (from `night_start`) at or above `target_low`, capped by
  the window and `max_run_min`

Each zone's result goes to the `decisions` table and a `slab_decision`
event, next to the `k` budget still left. At grade, the model is re-run from
the stored state with the heat and weather that actually happened
(`added`, `hind_low`), and compared with the actual low. That checks the
model's heat response.

For `live_zones` the decision does drive the charge: a live zone charges
the larger of its remaining `k` budget and the slab model's need (`target`
in the zone state; `session_start` logs both). Non-live zones are logged
only.

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
- `predictions`: shadow predictor output and scores (`model_low`,
  `slab_low`, `slab_t`)
- `decisions`: shadow charge decision at window open and its grade
- `loop_calls`: per heat call, the return dip, dip time, loop transit time
  and plateau
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

Shows the current plan and charge per zone, graded cycles (morning low, k change and reason), learned `k`, shadow predictions (old model, slab model and last night's low) with MAE, heat calls with the return dip and median loop transit per zone, the last 24 h of events, and problems found in the log.
