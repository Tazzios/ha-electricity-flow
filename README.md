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

Setting up the integration only asks whether you want the diagnostic sensors (changed
later via **Configure**). It creates the **House** device by itself. Then add your
installation one device at a time with **Add device** on the integration page:

- **Grid**: the grid import and the grid export meter (only once), and optionally the
  prices of grid to home and grid to battery.
- **Solar set**: a name, one solar energy meter and optionally the prices of solar to
  home, solar to grid and solar to battery. Add a solar set per inverter or roof.
- **Battery**: a name, one charge meter, one discharge meter and optionally the prices
  of battery to home and battery to grid.

Every price is set at the source of the flow, as a price entity or as a template.
The pickers only show energy sensors (Wh, kWh or MWh) as meter and prices per energy
(EUR/kWh, ct/kWh, EUR/MWh, ...) as price. Per quarter the flows are divided over the
solar sets and batteries by each one's share of the production, charge or discharge,
so the totals stay exactly the same. Change or remove a device with its menu on the
integration page.

An installation of version 0.8.0 or older is converted automatically: the grid meters
become the grid, every solar meter a solar set and the battery meters one battery.
Prices of older versions are removed: set them again on the devices.

## Sensors

With prices set, every flow gets a cost sensor for today and one for this month, on the
device the flow comes from (for example `sensor.ef_<device>_price_battery_home_day`).
The **House** device has the house costs (`sensor.ef_price_house_day` and `_month`):
the sum of solar, grid and battery to home. Every device also gets a total for today
and this month (`sensor.ef_<device>_price_total_day` and `_month`):

- **Solar set**: the value of all it delivered (to home, grid and battery).
- **Battery**: what it delivered (to home and grid) minus what charging it cost, at the
  price of the solar set or grid the energy came from. It can go down.
- **Grid**: what it delivered (to home and battery) minus the energy fed back (solar
  and battery to grid, at their price). It can go down.

With **Diagnostic sensors** switched on, every flow also gets a sensor with its energy
(kWh) in the current quarter, on the House and on every device, to check the
calculation. Switched off, these sensors are removed.

Problems with the selected sensors (missing or unavailable for over an hour, wrong unit,
price in another currency, one sensor selected twice) show up under
**Settings → System → Repairs** and clear by themselves once fixed.
See the wiki for
[all sensors](https://github.com/Tazzios/ha-electricity-flow/wiki/Sensors) and
[how costs are calculated](https://github.com/Tazzios/ha-electricity-flow/wiki/Cost-calculation).

## Development

```bash
pip install -r requirements_test.txt ruff
pytest
ruff check . && ruff format --check .
mypy
```

GitHub Actions run the same checks plus hassfest and the HACS validation.

To release: raise `version` in `manifest.json`, then push a tag `v<version>`;
the Release workflow creates the GitHub release with `electricity_flow.zip`, the file
HACS installs.

## License

MIT, see [LICENSE](LICENSE).
