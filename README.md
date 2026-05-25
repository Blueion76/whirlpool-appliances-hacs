# Whirlpool Appliances

A custom Home Assistant integration for Whirlpool cloud-connected appliances.

This integration is based on the Whirlpool Android app/API behavior and is intended to support Whirlpool, Maytag, KitchenAid, JennAir, Amana, and related Whirlpool cloud appliances, with extra focus on legacy Whirlpool cooking appliances such as oven/microwave combo units.

> This project is unofficial and is not affiliated with, endorsed by, or supported by Whirlpool Corporation.

> Please note this integration was made with AI.

## Features

- Home Assistant config flow setup
- Whirlpool account login
- US and EU Whirlpool cloud regions
- Appliance discovery
- Device/entity creation per appliance
- Oven and microwave combo support
- Oven target temperature number entity
- Oven start/modify behavior from the temperature number entity
- Stop oven button
- Stop microwave button
- Cavity light controls
- Microwave light and turntable controls where supported
- Control lock support
- Sabbath mode support where exposed by the appliance
- Raw status diagnostics
- Manual appliance time sync button
- Optional low scan interval support
- SAID shown in device information
- Whirlpool legacy API command services
- Experimental ThingShield/AWS IoT support

## Supported appliance types

This integration currently focuses on Whirlpool cloud appliances discovered from the Whirlpool mobile app API.

Known/targeted categories include:

- Cooking appliances
- Oven/microwave combo units
- Double ovens
- Legacy Minerva cooking models
- Appliances exposing legacy `setAttributes` status/control data

Other appliance categories may partially work if they expose compatible Whirlpool cloud attributes.

## Tested appliance example

Example tested model:

```text
Model: WOC54EC0HS00
Category: Cooking
Data model: DDM_COOKING_MINERVA_COMBO_BIO5_V1
```

## Installation with HACS

1. Open Home Assistant.
2. Open **HACS**.
3. Go to **Integrations**.
4. Open the three-dot menu.
5. Select **Custom repositories**.
6. Add this repository:

```text
https://github.com/Blueion76/whirlpool-appliances-hacs
```

7. Set category to:

```text
Integration
```

8. Click **Add**.
9. Install **Whirlpool Appliances**.
10. Restart Home Assistant.
11. Go to **Settings → Devices & services → Add integration**.
12. Search for **Whirlpool Appliances**.

## Manual installation

Copy this folder:

```text
custom_components/whirlpool_appliances
```

to your Home Assistant config directory:

```text
/config/custom_components/whirlpool_appliances
```

Then restart Home Assistant.

Your final path should look like:

```text
/config/custom_components/whirlpool_appliances/manifest.json
```

## Configuration

After installing and restarting Home Assistant:

1. Go to **Settings → Devices & services**.
2. Click **Add integration**.
3. Search for **Whirlpool Appliances**.
4. Enter your Whirlpool account email and password.
5. Select your region and brand.
6. Submit the setup form.

## Integration domain

The Home Assistant domain is:

```text
whirlpool_appliances
```

Services use this domain, for example:

```yaml
service: whirlpool_appliances.set_oven_cook
```

## Oven temperature control

Ovens use a Home Assistant **number entity** for target temperature control.

For US accounts, oven temperatures are shown in Fahrenheit.

The oven target temperature number entity supports:

```text
Minimum: 175°F
Maximum: 550°F
Step: 5°F
Mode: typed box input
```

Setting the oven target temperature number entity will start or modify the oven cook cycle.

If the oven already has a mode selected, the integration attempts to preserve that mode. If no mode is selected, it defaults to Bake.

Example behavior:

```text
Set oven target temperature to 300°F
→ Integration sends the closest Whirlpool Celsius command
→ Oven starts/modifies Bake at approximately 300°F
```

## Important oven temperature note

Whirlpool legacy ovens appear to store target temperatures internally in Celsius, even when the mobile app displays Fahrenheit. Because of this, some Fahrenheit values may be represented internally as the nearest whole Celsius value.

The integration rounds Fahrenheit setpoints to the closest Celsius-native value before sending the command.

Example:

```text
300°F → 149°C → approximately 300.2°F
```

The Home Assistant number entity snaps display values back to the nearest valid 5°F setpoint so the UI remains clean.

## Available entities

Entity availability depends on what your appliance exposes.

Common entities include:

- Online/status sensors
- Oven state sensor
- Microwave state sensor
- Oven target temperature number
- Oven current temperature sensor
- Oven target temperature sensor
- Control lock switch/sensor
- Sabbath mode switch/sensor
- Cavity light controls
- Microwave light controls
- Microwave turntable controls
- Stop oven button
- Stop microwave button
- Sync time button
- Raw status diagnostic sensor

Some entities are only created when the appliance reports matching attributes.

## Services

### Start or modify oven cook cycle

```yaml
service: whirlpool_appliances.set_oven_cook
data:
  said: WPRXXXXXXXXXX
  temperature: 149
  mode: bake
  cavity: upper
```

Temperature is sent in Celsius because that is what Whirlpool’s legacy API expects.

Common modes:

```text
bake
convect_bake
convection_bake
broil
convect_broil
convection_broil
convect_roast
convection_roast
keep_warm
air_fry
```

### Stop oven cavity

```yaml
service: whirlpool_appliances.stop_oven_cavity
data:
  said: WPRXXXXXXXXXX
  cavity: upper
```

### Stop microwave

```yaml
service: whirlpool_appliances.stop_microwave
data:
  said: WPRXXXXXXXXXX
```

### Set cavity light

```yaml
service: whirlpool_appliances.set_cavity_light
data:
  said: WPRXXXXXXXXXX
  cavity: upper
  on: true
```

### Sync appliance time

```yaml
service: whirlpool_appliances.sync_time
data:
  said: WPRXXXXXXXXXX
```

Optional timezone override:

```yaml
service: whirlpool_appliances.sync_time
data:
  said: WPRXXXXXXXXXX
  timezone: America/Chicago
```

### Set raw legacy attributes

Advanced/debug use only:

```yaml
service: whirlpool_appliances.set_attributes
data:
  said: WPRXXXXXXXXXX
  attributes:
    SomeAttribute: "1"
```

### Send raw appliance command

Advanced/debug use only:

```yaml
service: whirlpool_appliances.send_appliance_command
data:
  said: WPRXXXXXXXXXX
  command: setAttributes
  attributes:
    SomeAttribute: "1"
```

### Call Whirlpool API

Advanced/debug use only:

```yaml
service: whirlpool_appliances.call_api
data:
  method: GET
  path: /api/v1/appliance
```

## Finding your SAID

The appliance SAID is shown in the Home Assistant device information field as:

```text
Hardware version: SAID: WPRXXXXXXXXXX
```

It is also available in the raw appliance status data.

## Raw status diagnostics

The raw status entity exposes the Whirlpool cloud status payload for debugging.

This is useful when adding support for new appliances or troubleshooting missing entities.

When reporting issues, include:

- Appliance model number
- Appliance category
- Region
- Brand
- Relevant raw status attributes
- Home Assistant logs
- What entity/control is not working

Remove or redact any personal information before posting logs publicly.

## Troubleshooting

### Integration does not appear after install

Check that the folder exists here:

```text
/config/custom_components/whirlpool_appliances/manifest.json
```

Then restart Home Assistant.

### Entities are unavailable

Try:

1. Reload the integration.
2. Restart Home Assistant.
3. Confirm the appliance is online in the Whirlpool mobile app.
4. Check Home Assistant logs for Whirlpool errors.

### Old entities still appear

Home Assistant may keep old entity registry entries after updates.

Go to:

```text
Settings → Devices & services → Entities
```

Then delete stale entities that no longer exist.

### Login fails

Confirm that your Whirlpool credentials work in the official Whirlpool mobile app.

If login still fails, open an issue with:

- Region
- Brand
- Error message from Home Assistant logs
- Whether the same account works in the Whirlpool app

Do not post your password or full authentication tokens.

## Development

Clone the repository into your Home Assistant config directory:

```bash
cd /config
mkdir -p custom_components
git clone https://github.com/Blueion76/whirlpool-appliances-hacs.git /tmp/whirlpool-appliances-hacs
cp -r /tmp/whirlpool-appliances-hacs/custom_components/whirlpool_appliances custom_components/
```

Restart Home Assistant after making changes.

## Repository structure

```text
custom_components/
└── whirlpool_appliances/
    ├── __init__.py
    ├── api.py
    ├── binary_sensor.py
    ├── button.py
    ├── config_flow.py
    ├── const.py
    ├── coordinator.py
    ├── entity.py
    ├── light.py
    ├── manifest.json
    ├── number.py
    ├── select.py
    ├── sensor.py
    ├── services.yaml
    ├── strings.json
    ├── switch.py
    └── translations/
        └── en.json
```


## Disclaimer

This integration is unofficial and is not affiliated with Whirlpool Corporation.

Use remote appliance controls carefully. Starting ovens, microwaves, or cooking appliances remotely can create safety risks. Make sure the appliance is empty/safe to operate before sending commands.

## License

MIT License recommended.
