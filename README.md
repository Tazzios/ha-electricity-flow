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

| Entity id | Meaning |
|---|---|
| `sensor.ef_output_house_kwh` | House consumption |
| `sensor.ef_output_solar_home_kwh` | Solar → home |
| `sensor.ef_output_solar_battery_kwh` | Solar → battery |
| `sensor.ef_output_solar_grid_kwh` | Solar → grid |
| `sensor.ef_output_grid_home_kwh` | Grid → home |
| `sensor.ef_output_grid_battery_kwh` | Grid → battery |
| `sensor.ef_output_battery_home_kwh` | Battery → home |
| `sensor.ef_output_battery_grid_kwh` | Battery → grid |

With prices set, every flow also gets `sensor.ef_price_<flow>_day` and
`sensor.ef_price_<flow>_month`. See the wiki for
[all sensors](https://github.com/Tazzios/ha-electricity-flow/wiki/Sensors) and
[how costs are calculated](https://github.com/Tazzios/ha-electricity-flow/wiki/Cost-calculation).

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT, see [LICENSE](LICENSE).
