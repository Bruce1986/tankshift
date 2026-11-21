# tankshift

> An open-source simulator for building hot water systems: storage tanks, peak shower demand, time-of-use heating schedules and carbon.

[繁體中文說明](README.zh-TW.md)

**Status: design stage.** There is no runnable code yet. The design is in [`docs/DESIGN.md`](docs/DESIGN.md).

## Why

A hotel guest typically showers once or twice a day, yet the hot water system is kept hot around the clock, so part of the energy goes to standby and recirculation losses rather than to showers. How large that part is varies by building, and tankshift is meant to estimate it for a given site. At the other extreme, a school group checking in at night may need more hot water in one hour than the tank holds.

Manufacturer sizing calculators answer how big the equipment should be (see the design doc, §5). tankshift is meant to answer the questions that come after installation:

- How much does the system cost per year, and where does the energy go?
- If heating moves to off-peak hours, how much is saved, and does anyone get a cold shower?
- What happens on the night 40 students all shower between 21:00 and 22:00?

## Planned features (v1)

- **Venue-agnostic engine.** A venue is a preset of demand profile and defaults. v1 ships hotel and dormitory presets.
- **Single-node storage tank model**, following the energy balance of EnergyPlus `WaterHeater:Mixed`, at a one-minute time step.
- **Heat sources:** electric resistance, heat pump (COP varies with outdoor air and tank temperature) and gas boiler.
- **Time-of-use tariffs and carbon factors** for Taiwan and Australia, each value with its source and year.
- **Savings report** comparing a baseline with a modified scenario, in English and Traditional Chinese.
- **Legionella check:** hours below the storage temperature threshold are reported (a lower bound in v1, see the design doc), never traded away for savings.

Not in v1: stratified tanks, grid demand-response signals, booking-based automatic preheating, and any control of real equipment.

## Tech stack

TypeScript, published as a single npm package, so that the same engine runs in a CLI and in the browser.

## Roadmap

See the task list in [`project-handbook.md`](project-handbook.md#任務清單) (in Traditional Chinese).

## Contributing and contact

Questions, bug reports and venue data are welcome in [GitHub Issues](https://github.com/Bruce1986/tankshift/issues).

## License

[Apache-2.0](LICENSE)
