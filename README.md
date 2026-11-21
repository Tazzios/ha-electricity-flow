# Electricity Flow integration

Custom Home Assistant integration that calculates every 15 minutes how energy flows
between solar, battery, grid and home, and optionally what each flow costs or earns.

No utility meters, template sensors or helpers are needed: the integration keeps track
of the energy per quarter itself.

More explanation is in the [wiki](https://github.com/Tazzios/ha-electricity-flow/wiki).

## Installation

**HACS** (custom repository)

1. In HACS open the menu (three dots) → **Custom repositories**.
2. Add `https://github.com/Tazzios/ha-electricity-flow` with type **Integration**.
3. Install **Electricity Flow** and restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → Electricity Flow**.

**Manual**

1. Copy `custom_components/electricity_flow` to `/config/custom_components/electricity_flow`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Electricity Flow**.

## Configuration

Select your cumulative energy sensors (Wh, kWh or MWh):

- Grid import and grid export meter
- Solar production meters (zero or more)
- Battery charge and discharge meters (zero or more)

Optionally set a price per flow (a price sensor or a template) to get daily and
monthly cost sensors. Everything can be changed later via **Configure**.

## Sensors

Energy (kWh) per quarter for:

- House consumption
- Solar → home, solar → battery, solar → grid
- Grid → home, grid → battery
- Battery → home, battery → grid

With prices set, every flow also gets a cost sensor for today and one for this month.
See the wiki for
[all sensors](https://github.com/Tazzios/ha-electricity-flow/wiki/Sensors) and
[how costs are calculated](https://github.com/Tazzios/ha-electricity-flow/wiki/Cost-calculation).

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT, see [LICENSE](LICENSE).
