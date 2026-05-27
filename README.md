# Whirlpool Appliances

Home Assistant custom integration (HACS) for Whirlpool cloud-connected appliances.

> **Disclaimer**: This project is unofficial and is not affiliated with, endorsed by, or supported by Whirlpool Corporation. Made with AI.

## Supported brands

Whirlpool’s cloud platform is shared across multiple brands. This integration is intended to work with appliances connected through the official apps for:

- Whirlpool
- Maytag
- KitchenAid
- JennAir
- Amana

## Supported appliance types

The integration aims to support the most common Whirlpool-connected appliance categories, including:

- Washers / dryers (including laundry pairs)
- Dishwashers
- Refrigerators
- Ovens / ranges (including cavity light where supported)
- Microwaves

> Not every model exposes the same capabilities. Entities and services are created dynamically from the appliance capabilities/status returned by the cloud API.

## Tested appliances

The following appliances have been confirmed working by the community:

| Brand | Model | Region | Cloud type | Notes |
| --- | --- | --- | --- | --- |
| Whirlpool | WOC54EC0HS00 | US | SAID | Wall oven / microwave combo (reported working) |

If you have a confirmed working model, please open an issue or PR with:

- Brand + model number
- Region (US/EU)
- Appliance type
- Whether it is a legacy **SAID** appliance or a ThingShield **TS_SAID** appliance

## Features

- Region-aware login using the same mobile OAuth flow (`POST /oauth/token` with `grant_type=password`).
- Appliance discovery through account/location endpoints.
- Legacy status polling via `/api/v1/appliance/status/{said}`.
- ThingShield device support via OAuth → Cognito → AWS IoT MQTT, including:
  - state/update subscriptions
  - command response subscriptions
  - presence/online status
  - OTA status
  - capability responses
- Sensors and binary sensors for common state fields (cycle/phase/time remaining/temperatures/humidity/filter/fault/online/door/remote control/running/error).
- Optional raw diagnostic sensor (disabled by default).
- Services for major appliance actions observed in the Android app.
- MQTT command service for ThingShield devices (`whirlpool_appliances.publish_thing_command`) with `getState` as a safe default.
- Generic escape hatch service: `whirlpool_appliances.call_api`.
- REST command service: `whirlpool_appliances.send_appliance_command`.

## Installation (HACS)

1. Add this repository to HACS as a **Custom repository** (Integration).
2. Install **Whirlpool Appliances** from HACS.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and search for **Whirlpool Appliances**.
5. Sign in with your Whirlpool app account.

## Configuration

After setup, you can adjust options from:

**Settings → Devices & services → Whirlpool Appliances → Configure**

Typical options include polling frequency and diagnostic entity toggles.

## Usage

### Example service calls

```yaml
service: whirlpool_appliances.call_api
data:
  method: GET
  path: /api/v1/appliance/status/YOUR_SAID
```

```yaml
service: whirlpool_appliances.send_appliance_command
data:
  said: YOUR_SAID
  command: setCavityLight
  attributes:
    cavityLight: true
```

```yaml
service: whirlpool_appliances.appliance_function
data:
  function: check_firmware
  said: YOUR_SAID
```

```yaml
service: whirlpool_appliances.publish_thing_command
data:
  said: YOUR_TS_SAID
  command: getState
```

## Known limitations

- Whirlpool’s APIs and payloads are subject to change.
- Some older devices use legacy REST/STOMP patterns, while newer devices use ThingShield/AWS IoT.
- Stage/QA/dev/test endpoints are provided for debugging and should not be used unless you know exactly why you need them.

## Documentation

- Extracted API map: `docs/API_FINDINGS.md`

## Support

Please include diagnostics when reporting issues:

- Home Assistant version
- Integration version
- Appliance brand/model and region
- Whether your device uses **SAID** or **TS_SAID**
- Relevant logs (redact tokens)

## Development

CI checks used in this repository:

- `ruff format --check .`
- `ruff check .`
- Home Assistant `hassfest`

Optional local hooks:

```bash
pip install pre-commit
pre-commit install
```
