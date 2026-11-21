# Electricity Flow integration

Custom Home Assistant integration that calculates every 15 minutes how energy flows
between solar, battery, grid and home. It replaces the `electricity_flow.yaml` package from
[Tazzios/Home-Assistant](https://github.com/Tazzios/Home-Assistant/tree/main/Electricity%20flow).

No utility meters, template sensors or helpers are needed anymore: the integration
keeps track of the energy per quarter itself.

## Installation

If you used the `electricity_flow.yaml` package, remove (or disable) it and restart
Home Assistant **before** adding the integration, so the new sensors can take over the
old entity ids (see below).

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

Select the cumulative energy sensors (Wh, kWh or MWh are converted automatically):

| Field | Old package |
|---|---|
| Grid import meter | `source` of `ef_grid_import_kwh_quarter` |
| Grid export meter | `source` of `ef_grid_export_kwh_quarter` |
| Solar production meters (0..n) | `source` of `ef_solar_X_kwh_quarter` |
| Battery charge meters (0..n) | `source` of `ef_battery_charge_X_kwh_quarter` |
| Battery discharge meters (0..n) | `source` of `ef_battery_discharge_X_kwh_quarter` |

The sensors can be changed later via **Configure** on the integration.

## Sensors

Calculated flows (state class `total`, `last_reset` = start of the quarter, same as the package):

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

The entity ids are the same as in the package, so long term statistics and other
configuration (like `dynamic_energy_cost`) keep working, as long as the package is
removed before the integration is added.

Diagnostic sensors with the measured energy in the current quarter:
`sensor.ef_input_solar_kwh_quarter`, `sensor.ef_input_battery_charge_kwh_quarter`,
`sensor.ef_input_battery_discharge_kwh_quarter`, `sensor.ef_input_grid_import_kwh_quarter`,
`sensor.ef_input_grid_export_kwh_quarter` and `sensor.ef_grid_kwh_quarter` (import − export).

## Costs per flow (optional)

Setup and **Configure** use four pages: **Grid** (grid meters), **Solar** (solar meters;
prices of solar to home and solar to grid), **Battery** with the groups *Charge* (charge
meters; prices of solar to battery and grid to battery) and *Discharge* (discharge meters;
prices of battery to home and battery to grid), and **House** (prices of the house
consumption and grid to home). Per flow you can set a
price entity, for example a dynamic energy price sensor, or a template that returns the
price in currency per kWh, for example `{{ states('sensor.price') | float + 0.02 }}`
(not both). A template is re-rendered whenever the entities it uses change. Supported units of a price entity: `EUR/kWh`, `EUR/MWh`, `EUR/Wh`
and cents (`ct/kWh`); an entity without a unit is treated as currency per kWh.
Leave a field empty for no cost sensors (existing ones are then removed).

For every flow with a price two sensors are created (device class `monetary`,
state class `total`, currency of Home Assistant):

| Entity id | Meaning |
|---|---|
| `sensor.ef_price_<flow>_day` | Cost today, `last_reset` = local midnight |
| `sensor.ef_price_<flow>_month` | Cost this month, `last_reset` = first day of the month |

The name starts with the sign of the Home Assistant currency (for example "€ day – Grid to home"). Before 0.6.0 the entity ids were `sensor.ef_cost_*`; they are renamed automatically (history and statistics move along), unless you changed the entity id yourself.

`<flow>` is `house`, `solar_home`, `solar_battery`, `solar_grid`, `grid_home`,
`grid_battery`, `battery_home` or `battery_grid`.

How it is calculated:

- At the end of every quarter: cost = kWh of that quarter x **time weighted average
  price during the quarter**. Price updates that arrive a few seconds late or early
  at the quarter boundary therefore hardly matter.
- Time with an unavailable price is left out of the average. If the price was
  unavailable for the whole quarter, the last valid price is used (with a warning
  in the log). Without any valid price nothing is booked.
- The quarter 23:45-00:00 counts for the day (and month) it started in; after that
  the totals restart at 0.
- Negative prices give negative costs. For export flows (`solar_grid`,
  `battery_grid`) pick a feed-in price; the sensor then shows the revenue.
- The totals are restored after a restart (restore state). The running price
  average of the current quarter is stored too. A quarter that ends while Home
  Assistant is down is not booked.
- Attributes show the price entity (or template) and the kWh, price and cost of the last quarter.

## Differences with the package

- Recalculates on every change of a source sensor instead of every 30 seconds.
- Several solar or battery meters are summed per role, as in the package.
- A meter reset (for example a daily solar counter at midnight) is detected when the
  value drops below half of the previous value. Smaller drops are ignored as noise.
- After a restart within the same quarter the counters continue where they were.
  Energy measured while Home Assistant was down in another quarter is not assigned.

## Development

```bash
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT, see [LICENSE](LICENSE).
