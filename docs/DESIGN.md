# tankshift Design

<!-- Math follows the style guide's "wrap math in $" rule using GitHub's $`...`$ form for inline math and ```math blocks for display math. Plain $...$ breaks here: formulas with `_{` or `\,` are parsed as Markdown emphasis and escapes before the math renderer sees them (checked with GitHub's Markdown API on 2026-11-22). -->

**Document version:** 0.1 (v1 plan, nothing implemented yet)
**Date:** 2026-11-21
**Author:** Bruce Jhang

This document is for developers: why the engine is shaped the way it is. For what the project is and how to use it, see the [README](../README.md).

## Contents

- [1. Overview](#1-overview)
- [2. Architecture](#2-architecture) — [2.1 Data flow](#21-data-flow), [2.2 Modules](#22-modules), [2.3 Scenario file](#23-scenario-file-sketch), [2.4 Failure rules](#24-failure-rules-proposed)
- [3. Core model](#3-core-model) — [3.1 Tank](#31-tank-single-node-v1), [3.2 Heat sources](#32-heat-sources), [3.3 Verification](#33-verification)
- [4. Decisions](#4-decisions)
- [5. Prior art](#5-prior-art)
- [6. Known challenges](#6-known-challenges)
- [7. Future work](#7-future-work)

---

## 1. Overview

### 1.1 Vision

tankshift takes a venue, its hot water equipment and an electricity tariff, and reports what the system costs per year, how much a different heating schedule would save, and whether anyone would run out of hot water.

The primary audience is facility operators. They care about the savings, not the model, so the engine's main output is a savings report. The engine itself is written to be checked by engineers.

### 1.2 Design principles

- **One scenario file is the only interface.** The engine, venue presets, reports and the browser explainer all read and write the same scenario format.
- **Safety is a constraint, not a variable.** Storage temperature limits against Legionella are checked and reported. The engine never treats a lower setpoint as a free saving.
- **Every default has a source.** Tariffs, carbon factors and demand profiles carry their source and year in the data file.
- **Pessimistic where it matters.** When a simplification has a known bias, choose the direction that warns about cold showers rather than hides them, and document it.

### 1.3 v1 goals

1. Simulate one year at a one-minute step fast enough to run in a browser.
2. Close the energy balance at every step (see §3.3).
3. Compare a baseline and a modified scenario for a hotel and a dormitory under Taiwan and Australia tariffs.
4. Produce a report an operator can read without knowing the model.

---

## 2. Architecture

### 2.1 Data flow

```
scenario.json ──▶ ┌───────────────── tankshift ──────────────────┐ ──▶ savings report (HTML / Markdown / CSV, en + zh-TW)
                  │ demand   draw events, incl. group peaks      │ ──▶ hourly series (for charts)
                  │ tank     single-node storage, 1-min step     │
                  │ source   electric / heat pump / gas boiler   │
                  │ control  thermostat + heating windows        │
                  │ tariff   time-of-use prices + carbon factors │
                  │ report   baseline vs scenario comparison     │
                  └──────────────────────────────────────────────┘
                        ▲ the website explainer imports the same package
```

### 2.2 Modules

| Module | Responsibility |
|---|---|
| `demand` | Turns venue parameters (rooms, occupancy, persons per room, showers per person, shower length and flow, time-of-day shape, day-type and monthly multipliers, optional group event) into draw events. Day types (weekday, weekend, holiday) come from the venue's location calendar (time zone and public holidays, with sources), because time-of-use savings depend on how much demand falls on peak days. Each tariff template keeps its own day classification, which may differ from the location calendar. Deterministic mean profile plus seeded random events, so results are reproducible. |
| `tank` | Advances tank temperature one step given draws, heat input, standby loss and recirculation loop loss. Loop loss is modelled as a loss coefficient to the loop's surroundings (proposal, §4). |
| `source` | Heat delivered and energy consumed per step. Heat pump COP varies linearly with source air (outdoor) temperature and tank temperature. |
| `control` | Thermostat with deadband, setpoint and allowed heating windows by time of day. |
| `tariff` | Energy price and carbon factor per step, resolved from a calendar: season, weekday / weekend / holiday, and local time zone. v1 templates: Taiwan, Australia, plus gas price and carbon factor. Demand and capacity charges are out of v1; the report states that they are excluded, because a schedule change can affect them. |
| `report` | Annual cost, energy by end use, carbon, unmet hot water and Legionella-threshold hours, for baseline and scenario side by side. |

v1 is a single npm package with these as internal folders. Splitting into several packages waits until there is a reason.

### 2.3 Scenario file (sketch)

The exact schema is defined in T-03 to T-06. The shape is:

```json
{
  "venue": { "preset": "hotel", "rooms": 60, "occupancy": 0.8,
             "groupEvents": [{ "date": "2027-04-12", "start": "21:00", "people": 40, "windowMinutes": 60 }] },
  "tank": { "volumeL": 500, "setpointC": 60, "deadbandK": 5, "uaWPerK": 3.0, "surroundingsC": 22 },
  "source": { "type": "heatPump", "thermalOutputKW": 12, "copModel": { "...": "..." } },
  "recirculation": { "uaWPerK": 15, "surroundingsC": 25 },
  "control": { "heatingWindows": [["22:30", "07:30"]] },
  "tariff": { "template": "tw-taipower-tou-2stage" },
  "simulation": { "year": 2027, "stepMinutes": 1, "seed": 42 }
}
```

Units are fixed and appear as a suffix in field names: temperature °C (`…C`), temperature difference K (`…K`), volume L (`…L`), power W or kW (`…W`, `…KW`), energy kWh (`…KWh`), heat loss coefficient W/K (`…WPerK`), duration in minutes (`…Minutes`). A power field states whether it is thermal output or electrical input, as in `thermalOutputKW`.

### 2.4 Failure rules (proposed)

These rules keep a silent fallback from changing a headline number. They are proposals until confirmed in the task that implements them, and finer modelling choices are left to those tasks.

- **Validate before simulating.** A missing required field, a group event outside the simulated year, or $`T_{\text{tap}} \le T_{\text{mains}}`$ at any step is an error that names the field and the value. Nothing is silently dropped, wrapped or clamped.
- **Tariff and calendar coverage.** If a tariff, holiday list or temperature series does not cover every step of the simulated year, the engine stops and names the missing dates. It never substitutes a default day type or an average price.
- **Savings must be like for like.** A modified scenario that delivers less hot water spends less energy, and that is not a saving. A saving is reported as comparable only when the scenario delivers at least the baseline's tap heat ($`\int \dot{m}_{\text{tap}}\, c_p\, (T_{\text{delivered}} - T_{\text{mains}})\, dt`$, within a stated tolerance) and has no more unmet shower-minutes or Legionella-threshold hours. Otherwise the report marks it as not comparable and shows the heat shortfall in kWh next to it.
- **Same draws in both scenarios.** Draw events are generated from the venue, its location calendar, the simulated year and the seed only, never from tank, source, control or tariff fields, so baseline and scenario see identical demand. A comparison between scenarios with different venues, calendars or years is rejected. Because one seed is one possible year, the report either states the seed or, when several seeds are run, gives the saving as a median with its range.
- **Simulated year.** The year runs from local 1 January 00:00 to the next local 1 January 00:00 in the scenario's time zone. Coverage checks use that span.
- **Time grid.** Steps are UTC minutes. Heating windows, group events and demand shapes are resolved in the scenario's local civil time, so daylight saving days (Australia) have 23 or 25 hours of steps. On a transition day, a dated input (a group event) at a local time that does not exist or occurs twice is a validation error unless it states the offset. A recurring daily edge (a heating window, a demand shape) resolves deterministically instead: a nonexistent time moves to the next valid instant, a repeated time takes its first occurrence, and the report notes it. Taiwan has no daylight saving time. Each tariff template states the clock its periods follow (civil or standard time) with a source, and tariff periods are resolved on that clock; T-06 checks this for the chosen Australian tariff.
- **Heating windows are strict.** The heater never runs outside the allowed windows. When the tank falls short (a window ends below the setpoint, or showers go unmet), the report says so; the engine does not override the schedule. Whether a safety override should exist is an open question for T-03.
- **Undated series.** A typical-year series without dates (such as a TMY weather file) is aligned to the simulated year by month and day. In a leap year, 29 February repeats 28 February, and the report notes it.
- **Clamping is visible.** The COP model's inputs ($`T_{\text{air}}`$, $`T`$) are clamped to the range covered by the fitted operating points. Clamping a rectangle of inputs can still reach corners outside the fitted points, so the computed COP is also floored at 1.0, the value of resistance heating. The report shows, for baseline and scenario separately, the share of heat pump electricity computed with clamped inputs or a floored COP, as an assumption warning.

Which task confirms each proposal in §2.2, §2.3, §2.4 and §3:

| Proposal | Confirmed in |
|---|---|
| Scenario schema framework (§2.3; T-04 to T-06 add their own fields), SI time units in the solver, validation framework, mixing-valve model and unmet definition, heating windows strict or with a safety override, loop loss coefficient and default | T-03 |
| Heat pump COP and output fits, input clamping and the COP floor, the four environment temperature inputs and how an undated typical-year series maps to the simulated year | T-04 |
| $`T_{\text{tap}}`$ default, draw generation independent of tank, source, control and tariff, day-type and monthly multipliers | T-05 |
| Location calendar and each tariff's own day classification, holiday sources, simulated year, time grid and daylight saving rules, tariff clock basis, coverage checks, gas price and carbon | T-06 |
| Unmet tolerance, Legionella threshold and lower-bound reporting, like-for-like comparison, seed and clamping reporting | T-07 |

Until a value is confirmed, earlier tasks use a placeholder: T-03 tests unmet detection with a placeholder tolerance and constant temperatures, and T-05 uses a placeholder calendar until T-06.

---

## 3. Core model

### 3.1 Tank: single node (v1)

The tank is one fully mixed volume. Per step, the energy balance follows the form used by EnergyPlus `WaterHeater:Mixed` ([Engineering Reference](https://bigladdersoftware.com/epx/docs/9-6/engineering-reference/water-thermal-tanks-includes-water-heaters.html)), with a recirculation loop added:

```math
m\, c_p \frac{dT}{dt} = q_{\text{heater}} + UA\,(T_{\text{surr}} - T) + UA_{\text{loop}}\,(T_{\text{loop}} - T) - q_{\text{draw}}
```

**Draws and delivery temperature.** Demand is specified at the tap: tap flow $`\dot{m}_{\text{tap}}`$ and a target delivery temperature $`T_{\text{tap}}`$, with $`T_{\text{tap}} > T_{\text{mains}}`$ required. A thermostatic mixing valve blends tank water with mains water:

- While $`T \ge T_{\text{tap}}`$, the tank supplies the fraction $`\frac{T_{\text{tap}} - T_{\text{mains}}}{T - T_{\text{mains}}}`$ of the tap flow (between 0 and 1) and the rest is mains water. The heat leaving the tank is then the constant $`q_{\text{draw}} = \dot{m}_{\text{tap}}\, c_p\, (T_{\text{tap}} - T_{\text{mains}})`$, independent of $`T`$.
- While $`T < T_{\text{tap}}`$, the tank supplies the whole tap flow, so $`q_{\text{draw}} = \dot{m}_{\text{tap}}\, c_p\, (T - T_{\text{mains}})`$ and the water is delivered at $`T`$. That shower-minute counts as **unmet** when $`T`$ is below $`T_{\text{tap}}`$ by more than a tolerance.

The default $`T_{\text{tap}}`$ and the tolerance are proposals to be settled in T-05 and T-07. The report counts unmet shower-minutes and the number of people affected, by hour.

$`T_{\text{surr}}`$ is the temperature around the tank and $`T_{\text{loop}}`$ the temperature around the recirculation loop (§3.2). $`m`$ is the mass of water in the tank (from `tank.volumeL`), $`c_p`$ the specific heat of water, $`UA`$ and $`UA_{\text{loop}}`$ the loss coefficients of the tank and the loop (`tank.uaWPerK`, `recirculation.uaWPerK`), and $`q_{\text{heater}}`$ the source's thermal output while on (§3.2) and 0 while off.

**Solving a step.** Draws, the environment temperatures (§3.2) and tariff values are piecewise constant on the one-minute grid, so within a step they do not change. Within one regime the equation is then $`dT/dt = a + bT`$ with constant $`a`$ and $`b`$:

```math
k = \begin{cases} \dot{m}_{\text{tap}}\, c_p & T < T_{\text{tap}} \\ 0 & \text{otherwise} \end{cases}
\qquad
q_{\text{valve}} = \begin{cases} \dot{m}_{\text{tap}}\, c_p\, (T_{\text{tap}} - T_{\text{mains}}) & T \ge T_{\text{tap}} \\ 0 & \text{otherwise} \end{cases}
```

```math
b = -\frac{UA + UA_{\text{loop}} + k}{m\, c_p} \le 0
\qquad
a = \frac{q_{\text{heater}} + UA\, T_{\text{surr}} + UA_{\text{loop}}\, T_{\text{loop}} + k\, T_{\text{mains}} - q_{\text{valve}}}{m\, c_p}
```

The solver works in SI units: $`t`$ in seconds, $`b`$ in 1/s and $`a`$ in K/s, so a one-minute step is $`t = 60`$ s. Scenario fields in minutes (`stepMinutes`, `windowMinutes`) are converted once at input; mixing minutes into $`e^{bt}`$ would be off by a factor of 60.

The closed-form solution is $`T(t) = (T_0 + a/b)\, e^{bt} - a/b`$, which decays towards $`-a/b`$. When $`b = 0`$ the solution is $`T(t) = T_0 + a t`$. The form above loses accuracy when $`b`$ is close to 0 ($`a/b`$ becomes large and cancels), so the implementation uses the equivalent form $`T(t) = T_0 + (a + b T_0)\, \operatorname{expm1}(bt)/b`$, taking $`\operatorname{expm1}(bt)/b`$ as $`t`$ when $`b = 0`$.

The solution is exact within a regime, because while on, v1 heat sources deliver a thermal output that is constant within a step (§3.2). The step is split wherever the regime changes inside it: when the thermostat switches, and when $`T`$ crosses $`T_{\text{tap}}`$. Only heat pump electricity is approximate: its COP depends on $`T`$, and v1 evaluates it at the start of each sub-step.

The model is to be implemented from the public EnergyPlus documentation only; EcosizerEngine source is not read (§5).

#### Known biases of the single node

- *Cold showers: pessimistic.* A fully mixed tank lowers outlet temperature as soon as cold water enters, while a real stratified tank keeps delivering hot water until the cold layer reaches the outlet. The model is expected to under-predict how much hot water a tank can deliver in a peak. That is the safe direction for warning about cold showers, but it overstates the need for a bigger tank.
- *Legionella: optimistic.* A real tank has a cooler bottom zone that can sit below the threshold while the mean temperature is above it. v1 Legionella-threshold hours are therefore reported as a lower bound.
- *Heat pump below its coldest published point: optimistic.* Clamped inputs (§2.4) hold COP and output at the values of the coldest published point, which a real unit no longer reaches. The clamping share in the report shows how much of the result depends on it.

A stratified model is the planned second step.

### 3.2 Heat sources

- **Electric resistance:** rated thermal output while on; heat equals electricity.
- **Heat pump:** thermal output while on is a linear function of source air temperature $`T_{\text{air}}`$ (proposal, T-04), so a cold night delivers less heat; $`T_{\text{air}}`$ is constant within a step, so output is too. Electricity = heat / COP. COP is a linear function of $`T_{\text{air}}`$ and tank temperature, with clamped inputs (§2.4). A constant COP was rejected because it would make night-time heating look better than it is.
- **Gas boiler:** rated thermal output while on, fixed efficiency on fuel energy. Its cost and carbon need a gas price and a gas carbon factor, which T-06 supplies alongside the electricity tariffs.

Fitting the linear COP model $`\mathrm{COP} = c_0 + c_1 T_{\text{air}} + c_2 T`$ needs at least three published operating points, not all on one line in the $`(T_{\text{air}}, T)`$ plane.

**Environment temperatures vary over the year.** The scenario takes four temperature inputs, each as an annual series or a seasonal profile with a documented source:

- $`T_{\text{surr}}`$, the temperature around the tank, which drives standby loss;
- $`T_{\text{loop}}`$, the temperature around the recirculation loop, which drives loop loss (it may reuse the $`T_{\text{surr}}`$ series);
- $`T_{\text{air}}`$, the heat pump's source air temperature, which is outdoor air for an outdoor unit;
- $`T_{\text{mains}}`$, the mains water temperature, which sets how much heat each litre needs.

A single constant would hide exactly the winter-night effect that D6 exists to capture. The constant `surroundingsC` fields in the §2.3 sketch are placeholders for these inputs.

### 3.3 Verification

- **Energy balance invariant.** At every step: heat added = heat leaving with draws ($`q_{\text{draw}}`$, in whichever regime applies) + standby loss + recirculation loss + change in stored heat. Losses and draw heat are the exact integrals of the closed-form $`T(t)`$ over each sub-step, and stored heat is $`m\, c_p\, T`$, so the tolerance only covers floating-point round-off, not discretisation error. A model that drops or double-counts a term fails this check.
- **Analytic cases.** Pure standby cooling and pure heating from a known start temperature are compared with hand-calculated results.
- **Mutation testing** of the invariant tests, to show they fail when the model is broken.
- Comparison with EnergyPlus output is planned, but needs EnergyPlus to be downloaded and run, so it waits for owner approval.

---

## 4. Decisions

| # | Decision | Date |
|---|---|---|
| D1 | Primary users are facility operators; the author acts as the engineer and delivers savings reports. | 2026-11-20 |
| D2 | First purpose module is time-of-use scheduling, with carbon computed alongside. | 2026-11-20 |
| D3 | v1 venues: hotel and dormitory. | 2026-11-20 |
| D4 | Single-node tank first; stratified tank second. | 2026-11-20 |
| D5 | README, design docs and code in English, plus `README.zh-TW.md`. `WORKLOG.md` and `project-handbook.md` stay in Traditional Chinese. | 2026-11-21 |
| D6 | Heat pump COP varies linearly with source air (outdoor) temperature and tank temperature. | 2026-11-21 |
| D7 | Demand = mean profile plus seeded random events. | 2026-11-21 |
| D8 | Repository is public from day one. | 2026-11-21 |
| D9 | One npm package with internal folders. | 2026-11-21 |
| — | v1 architecture as drafted: one-minute step, electric / heat pump / gas sources, report in HTML, Markdown and CSV (en + zh-TW), CLI plus npm package. Still proposals, added during review: the scenario schema (§2.3); the mixing-valve model, cold-shower definition and their defaults, the loop loss coefficient, environment inputs and Legionella lower-bound reporting (§3); tariff calendar details (§2.2); the failure rules (§2.4). | 2026-11-21 |
| — | License Apache-2.0, for its explicit patent grant. | 2026-11-21 |
| — | v1 tariff templates: Taiwan and Australia only. | 2026-11-21 |
| — | v1 has no booking-based automatic preheating (see §5). User-defined scenarios such as "40 people at 21:00" are in scope. | 2026-11-21 |
| — | Equipment prices are entered by the user; no vendor quotes. | 2026-11-21 |
| — | Project materials make no "first" or "only" claims. | 2026-11-21 |

---

## 5. Prior art

A prior-art survey was done in November 2026. The points below shaped the design. The licenses of OCHRE, HPWHsim and OpenDHW were checked with GitHub's license detection on 2026-11-20. Feature descriptions come from the survey and were not independently re-checked, except OpenDHW, which the survey did not list: its description is the repository's own one-line description.

- [EcosizerEngine](https://github.com/EcotopeResearch/EcosizerEngine) simulates multi-family and commercial heat pump water heater systems with load shifting. GitHub detects no license, and the repository root has no LICENSE file, but its README states that it is released under the GNU Affero General Public License. tankshift therefore treats it as AGPL and does not read or copy its source.
- [OCHRE](https://github.com/NatLabRockies/OCHRE) (BSD-3-Clause) models residential water heaters with load-up and shed control, built around residential HPXML inputs.
- [HPWHsim](https://github.com/bigladder/HPWHsim) (BSD-3-Clause) is a detailed heat pump water heater physics core.
- [OpenDHW](https://github.com/RWTH-EBC/OpenDHW) (MIT) generates household hot water draw profiles. To be reviewed for T-05.
- Manufacturer sizing calculators size equipment for a peak hour but do not simulate schedules, tariffs or group arrivals.
- Patent [US8600563](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/8600563) ("Hybrid heating system"; the survey lists [EP2356387B1](https://patents.google.com/patent/EP2356387B1) in the same family) describes raising stored heat from a demand forecast, with an example of hotel staff entering about 100 expected arrivals. This description is taken from the survey and search excerpts; the full text was not re-read. Its legal status and claim scope have not been assessed, and nothing here is legal advice. Automatic booking-based preheating is left out of v1 as a precaution.

---

## 6. Known challenges

- **Demand data.** The survey found no freely licensed, high-resolution hotel or dormitory draw data. v1 defaults will be documented assumptions. Two leads for T-05: the U.S. DOE commercial prototype building models, which according to the survey include hourly hot water schedules for small and large hotels (not yet verified), and OpenDHW (§5; not in the survey, only its license and description checked). Downloading either needs owner approval first.
- **Recirculation loss** varies widely between buildings and is hard to estimate without measurement. v1 takes the loop's loss coefficient as a user input with a documented default.
- **Heat pump performance** differs by product. The linear COP model needs at least three published operating points per product (§3.2).

---

## 7. Future work

Stratified tank model; grid demand-response signals; more tariff templates (Japan, California); more venues (locker rooms, mountain huts, care homes, shelters); drain water heat recovery payback; the website explainer (T-09).
