# Whirlpool Appliances

A custom Home Assistant integration for Whirlpool cloud-connected appliances.

This integration is based on the Whirlpool Android app/API behavior and is intended to support Whirlpool, Maytag, KitchenAid, JennAir, Amana, and related Whirlpool cloud appliances, with extra focus on legacy Whirlpool cooking appliances such as oven/microwave combo units.

> This project is unofficial and is not affiliated with, endorsed by, or supported by Whirlpool Corporation.

> Please note this integration was made with AI.

## What it supports

- Config flow login now follows the same `/oauth/token` password-grant path used by `MizterB/homeassistant-whirlpool` / `whirlpool-sixth-sense`, with account-locked handling and same-region brand credential fallback.
- US/EU production base URLs observed in the APK, plus stage/QA/dev/test/custom for debugging.
- Appliance discovery through account/location endpoints.
- Status polling from `/api/v1/appliance/status/{said}` for legacy `SAID` appliances.
- ThingShield `TS_SAID` support through OAuth → Cognito → AWS IoT MQTT, including state/update, command response, presence, OTA status, and capability response subscriptions.
- Generic sensors/binary sensors for state, cycle, phase, time remaining, temperatures, humidity, filter, fault, online, door, remote control, running, and error.
- Raw diagnostic status sensor, disabled by default.
- Services for all major appliance API actions discovered in the APK.
- `whirlpool_appliances.publish_thing_command` for ThingShield MQTT commands, with `getState` as the safe default.
- Generic service escape hatch: `whirlpool_appliances.call_api`.
- Command service: `whirlpool_appliances.send_appliance_command` using `/api/v1/appliance/command`.
- Cavity light wrapper using the APK-observed `setCavityLight`/`cavityLight` strings.


## Login behavior

This build intentionally removed the extra guessed APK login endpoints and uses the proven mobile OAuth flow: `POST /oauth/token` with `grant_type=password`, the selected region base URL, the Android `okhttp/3.12.0` user agent, and the public Whirlpool/Maytag/KitchenAid mobile client credentials. If the selected brand fails, the integration tries other known client credentials for the same region before showing an auth error.

## Installation

1. Copy `custom_components/whirlpool_appliances` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add integration: **Settings → Devices & services → Add integration → Whirlpool Appliances**.
4. Use your Whirlpool app account credentials.

## Important limitations

This was derived from a static APK inspection, not from a live Whirlpool account session. Whirlpool uses obfuscated mobile code, changing DTO payloads, legacy REST/STOMP for older `SAID` devices, and AWS IoT MQTT for newer `TS_SAID` devices. The integration is therefore intentionally data-driven: it exposes all discovered API paths and robust raw-status entities, but some command payloads may require adjustment from Home Assistant logs for your exact appliance model.

Do not use stage/QA/dev environments unless you know why you need them.

## Example service calls

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

See `docs/API_FINDINGS.md` for the extracted API map.



## Disclaimer

This integration is unofficial and is not affiliated with Whirlpool Corporation.

Use remote appliance controls carefully. Starting ovens, microwaves, or cooking appliances remotely can create safety risks. Make sure the appliance is empty/safe to operate before sending commands.
