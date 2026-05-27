"""Service registration for Whirlpool Appliances."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .api import WhirlpoolCloudClient
from .api_spec import APPLIANCE_FUNCTIONS
from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN
from .helpers.logging import summarize


def register_services(hass: HomeAssistant) -> None:
    """Register Whirlpool Appliances services."""
    if hass.services.has_service(DOMAIN, "call_api"):
        return

    async def call_api(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        path = call.data["path"]
        if not path.startswith("/") and not path.startswith(client.base_url):
            raise HomeAssistantError(
                "Path must be a Whirlpool API path starting with '/' or the configured base URL"
            )
        return _service_result(
            await client.request(
                call.data.get("method", "GET"),
                path,
                json=call.data.get("body"),
                params=call.data.get("params"),
                auth=call.data.get("auth", True),
            )
        )

    async def send_command(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.send_appliance_command(
            _service_said(hass, call),
            call.data.get("command", "setAttributes"),
            call.data.get("attributes"),
            call.data.get("raw"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_cavity_light(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_cavity_light(
            _service_said(hass, call),
            _service_bool(call),
            call.data.get("cavity"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def publish_thing_command(call: ServiceCall) -> dict[str, Any]:
        result = await _first_coordinator(hass).async_publish_thing_command(
            _service_said(hass, call),
            call.data.get("command", "getState"),
            call.data.get("payload"),
        )
        return _service_result(result)

    async def set_attributes(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.send_attributes(
            _service_said(hass, call),
            call.data["attributes"],
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_oven_cook(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_oven_cook(
            _service_said(hass, call),
            call.data["temperature"],
            call.data.get("mode", "bake"),
            call.data.get("cavity", "upper"),
            cook_time_seconds=_minutes_to_seconds(call.data.get("cook_time_minutes")),
            delay_time_seconds=_minutes_to_seconds(call.data.get("delay_time_minutes")),
            complete_action=call.data.get("complete_action", "turn_off"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_oven_frozen_bake(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_oven_frozen_bake(
            _service_said(hass, call),
            call.data["food"],
            call.data["temperature"],
            int(round(float(call.data["cook_time_minutes"]) * 60)),
            call.data.get("cavity", "upper"),
            complete_action=call.data.get("complete_action", "turn_off"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def stop_oven_cavity(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.stop_oven_cavity(
            _service_said(hass, call),
            call.data.get("cavity", "upper"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def stop_microwave(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.stop_microwave(_service_said(hass, call))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_quiet_mode(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_quiet_mode(_service_said(hass, call), _service_bool(call))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_remote_enable(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_remote_enable(_service_said(hass, call), _service_bool(call))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_kitchen_timer(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_kitchen_timer(
            _service_said(hass, call),
            call.data["seconds"],
            call.data.get("timer", 1),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def stop_kitchen_timer(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.stop_kitchen_timer(
            _service_said(hass, call),
            call.data.get("timer", 1),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def check_firmware_update(call: ServiceCall) -> dict[str, Any]:
        return _service_result(
            await _first_client(hass).check_firmware_update(_service_said(hass, call))
        )

    async def sync_time(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.sync_appliance_time(
            _service_said(hass, call),
            call.data.get("timezone"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_time_auto_update(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_time_auto_update(
            _service_said(hass, call),
            call.data["enabled"],
            call.data.get("timezone"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_timezone(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        timezone = str(call.data["timezone"])
        result = await client.send_attributes(
            _service_said(hass, call),
            {
                "TimeZoneId": timezone,
                "TimezoneId": timezone,
                "XCat_TimeZoneId": timezone,
            },
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def refresh(call: ServiceCall) -> None:
        await _first_coordinator(hass).async_request_refresh()

    async def refresh_ddm_capabilities(call: ServiceCall) -> dict[str, Any]:
        coordinator = _first_coordinator(hass)
        await coordinator.async_fetch_ddm_capabilities(force=True)
        data = dict(coordinator.data or {})
        data["ddm_capabilities"] = coordinator._ddm_capabilities
        data["ddm_errors"] = coordinator._ddm_errors
        coordinator.async_set_updated_data(data)
        return _service_result(
            {
                "ddm_keys": list(coordinator._ddm_capabilities),
                "errors": coordinator._ddm_errors,
            }
        )

    async def appliance_function(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.call_function(
            call.data["function"],
            said=_service_said(hass, call),
            body=call.data.get("body"),
            **dict(call.data.get("path_values") or {}),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    _register(
        hass,
        "call_api",
        call_api,
        vol.Schema(
            {
                vol.Required("path"): str,
                vol.Optional("method", default="GET"): str,
                vol.Optional("body"): object,
                vol.Optional("params"): object,
                vol.Optional("auth", default=True): bool,
            }
        ),
    )
    _register(
        hass,
        "send_appliance_command",
        send_command,
        _said_schema(
            {
                vol.Optional("command", default="setAttributes"): str,
                vol.Optional("attributes"): object,
                vol.Optional("raw"): object,
            }
        ),
    )
    _register(
        hass,
        "set_cavity_light",
        set_cavity_light,
        _said_schema({vol.Required("enabled"): bool, vol.Optional("on"): bool, vol.Optional("cavity"): str}),
    )
    _register(
        hass,
        "publish_thing_command",
        publish_thing_command,
        _said_schema({vol.Optional("command", default="getState"): str, vol.Optional("payload"): object}),
    )
    _register(hass, "set_attributes", set_attributes, _said_schema({vol.Required("attributes"): dict}))
    _register(
        hass,
        "set_oven_cook",
        set_oven_cook,
        _said_schema(
            {
                vol.Required("temperature"): vol.Coerce(float),
                vol.Optional("mode", default="bake"): vol.In(
                    [
                        "bake",
                        "convect_bake",
                        "convection_bake",
                        "broil",
                        "convect_broil",
                        "convection_broil",
                        "convect_roast",
                        "convection_roast",
                        "keep_warm",
                        "air_fry",
                    ]
                ),
                vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"]),
                vol.Optional("cook_time_minutes"): vol.Coerce(float),
                vol.Optional("delay_time_minutes"): vol.Coerce(float),
                vol.Optional("complete_action", default="turn_off"): vol.In(["turn_off", "keep_warm", "stay_on"]),
            }
        ),
    )
    _register(
        hass,
        "set_oven_frozen_bake",
        set_oven_frozen_bake,
        _said_schema(
            {
                vol.Required("food"): vol.In(["pizza", "pie", "meals", "fries", "nuggets", "lasagna"]),
                vol.Required("temperature"): vol.Coerce(float),
                vol.Required("cook_time_minutes"): vol.Coerce(float),
                vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"]),
                vol.Optional("complete_action", default="turn_off"): vol.In(["turn_off", "keep_warm", "stay_on"]),
            }
        ),
    )
    _register(hass, "stop_oven_cavity", stop_oven_cavity, _said_schema({vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"])}))
    _register(hass, "stop_microwave", stop_microwave, _said_schema())
    _register(hass, "set_quiet_mode", set_quiet_mode, _bool_schema())
    _register(hass, "set_remote_enable", set_remote_enable, _bool_schema())
    _register(hass, "set_kitchen_timer", set_kitchen_timer, _said_schema({vol.Required("seconds"): vol.Coerce(int), vol.Optional("timer", default=1): vol.Coerce(int)}))
    _register(hass, "stop_kitchen_timer", stop_kitchen_timer, _said_schema({vol.Optional("timer", default=1): vol.Coerce(int)}))
    _register(hass, "check_firmware_update", check_firmware_update, _said_schema())
    _register(hass, "sync_time", sync_time, _said_schema({vol.Optional("timezone"): str}))
    _register(hass, "set_time_auto_update", set_time_auto_update, _said_schema({vol.Required("enabled"): bool, vol.Optional("timezone"): str}))
    _register(hass, "set_timezone", set_timezone, _said_schema({vol.Required("timezone"): str}))
    hass.services.async_register(DOMAIN, "refresh", refresh)
    _register(hass, "refresh_ddm_capabilities", refresh_ddm_capabilities, None)
    _register(
        hass,
        "appliance_function",
        appliance_function,
        _said_schema(
            {
                vol.Required("function"): vol.In(list(APPLIANCE_FUNCTIONS)),
                vol.Optional("body"): object,
                vol.Optional("path_values"): object,
            }
        ),
    )


def unregister_services(hass: HomeAssistant) -> None:
    """Unregister Whirlpool Appliances services."""
    for service in (
        "call_api",
        "send_appliance_command",
        "set_cavity_light",
        "publish_thing_command",
        "set_attributes",
        "set_oven_cook",
        "set_oven_frozen_bake",
        "stop_oven_cavity",
        "stop_microwave",
        "set_quiet_mode",
        "set_remote_enable",
        "set_kitchen_timer",
        "stop_kitchen_timer",
        "check_firmware_update",
        "sync_time",
        "set_time_auto_update",
        "set_timezone",
        "refresh",
        "refresh_ddm_capabilities",
        "appliance_function",
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _register(
    hass: HomeAssistant,
    service: str,
    handler,
    schema: vol.Schema | None,
) -> None:
    kwargs: dict[str, Any] = {"supports_response": SupportsResponse.OPTIONAL}
    if schema is not None:
        kwargs["schema"] = schema
    hass.services.async_register(DOMAIN, service, handler, **kwargs)


def _said_schema(extra: dict[Any, Any] | None = None) -> vol.Schema:
    data: dict[Any, Any] = {
        vol.Optional("appliance_device"): str,
        vol.Optional("said"): str,
    }
    data.update(extra or {})
    return vol.Schema(data)


def _bool_schema() -> vol.Schema:
    return _said_schema(
        {
            vol.Required("enabled"): bool,
            vol.Optional("on"): bool,
        }
    )


def _first_client(hass: HomeAssistant) -> WhirlpoolCloudClient:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("No Whirlpool Appliances config entry is loaded")
    return next(iter(entries.values()))[DATA_CLIENT]


def _first_coordinator(hass: HomeAssistant):
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("No Whirlpool Appliances config entry is loaded")
    return next(iter(entries.values()))[DATA_COORDINATOR]


def _service_said(hass: HomeAssistant, call: ServiceCall) -> str:
    said = call.data.get("said")
    if said:
        return str(said)

    device_id = call.data.get("appliance_device")
    if isinstance(device_id, list):
        device_id = device_id[0] if device_id else None
    if device_id:
        resolved = _appliance_said_from_device(hass, str(device_id))
        if resolved:
            return resolved

    raise HomeAssistantError("Select a Whirlpool appliance device or enter a SAID")


def _appliance_said_from_device(hass: HomeAssistant, device_id: str) -> str | None:
    device = dr.async_get(hass).async_get(device_id)
    if not device:
        return None
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier:
            return str(identifier)
    return None


def _service_bool(call: ServiceCall, key: str = "enabled") -> bool:
    if key in call.data:
        return bool(call.data[key])
    if "on" in call.data:
        return bool(call.data["on"])
    raise HomeAssistantError(f"Missing required boolean field: {key}")


def _minutes_to_seconds(value: Any) -> int | None:
    if value in (None, ""):
        return None
    minutes = float(value)
    return int(round(minutes * 60)) if minutes > 0 else None


def _service_result(result: Any) -> dict[str, Any]:
    if result is False:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="request_failed")
    if isinstance(result, dict):
        status = str(result.get("status", "")).strip().lower()
        message = str(result.get("message", "")).strip().lower()
        if status in {"error", "failed", "fail", "02", "2", "nack"} or "negative acknow" in message:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="request_failed")
    return {"result": summarize(result)}
