"""Service registration for Whirlpool Appliances."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .api import WhirlpoolApiError, WhirlpoolCloudClient
from .api_spec import APPLIANCE_FUNCTIONS
from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN


def register_services(hass: HomeAssistant) -> None:
    """Register Whirlpool Appliances services."""
    if hass.services.has_service(DOMAIN, "call_api"):
        return

    async def call_api(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        path = call.data["path"]
        if not path.startswith("/") and not path.startswith(client.base_url):
            raise HomeAssistantError("Path must be a Whirlpool API path starting with '/' or the configured base URL")
        return _response(await client.request(call.data.get("method", "GET"), path, json=call.data.get("body"), params=call.data.get("params"), auth=call.data.get("auth", True)))

    async def send_command(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.send_appliance_command(_service_said(hass, call), call.data.get("command", "setAttributes"), call.data.get("attributes"), call.data.get("raw"))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_cavity_light(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_cavity_light(_service_said(hass, call), _service_bool(call), call.data.get("cavity"))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def publish_thing_command(call: ServiceCall) -> dict[str, Any]:
        result = await _first_coordinator(hass).async_publish_thing_command(_service_said(hass, call), call.data.get("command", "getState"), call.data.get("payload"))
        return _service_result(result)

    async def set_attributes(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.send_attributes(_service_said(hass, call), call.data["attributes"])
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
        result = await client.set_oven_frozen_bake(_service_said(hass, call), call.data["food"], call.data["temperature"], int(round(float(call.data["cook_time_minutes"]) * 60)), call.data.get("cavity", "upper"), complete_action=call.data.get("complete_action", "turn_off"))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def stop_oven_cavity(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.stop_oven_cavity(_service_said(hass, call), call.data.get("cavity", "upper"))
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
        result = await client.set_kitchen_timer(_service_said(hass, call), call.data["seconds"], call.data.get("timer", 1))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def stop_kitchen_timer(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.stop_kitchen_timer(_service_said(hass, call), call.data.get("timer", 1))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def check_firmware_update(call: ServiceCall) -> dict[str, Any]:
        return _service_result(await _first_client(hass).check_firmware_update(_service_said(hass, call)))

    async def sync_time(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.sync_appliance_time(_service_said(hass, call), call.data.get("timezone"))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_time_auto_update(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.set_time_auto_update(_service_said(hass, call), call.data["enabled"], call.data.get("timezone"))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_timezone(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        timezone = str(call.data["timezone"])
        result = await client.send_attributes(_service_said(hass, call), {"TimeZoneId": timezone, "TimezoneId": timezone, "XCat_TimeZoneId": timezone})
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
        return _response({"ddm_keys": list(coordinator._ddm_capabilities), "errors": coordinator._ddm_errors})

    async def update_appliances(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.request("POST", "/api/v2/updateAppliances", json=call.data.get("body"))
        await _first_coordinator(hass).async_request_refresh()
        return _response(result)

    async def get_ddm_content(call: ServiceCall) -> dict[str, Any]:
        ddm_key = str(call.data["ddm_key"])
        return _response(await _first_client(hass).request("GET", f"/api/v1/contents/all/{ddm_key}"))

    async def appliance_function(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.call_function(call.data["function"], said=_service_said(hass, call), body=call.data.get("body"), **dict(call.data.get("path_values") or {}))
        await _first_coordinator(hass).async_request_refresh()
        return _response(result)

    async def get_cycle_history(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", "/api/v1/history/cycle", params=_history_params(hass, call) or None))

    async def get_fault_history(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", "/api/v1/history/faultCode", params=_history_params(hass, call) or None))

    async def get_favorites(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        said = _service_said(hass, call)
        try:
            result = await client.request("GET", f"/api/v2/account/favorites/{said}")
        except WhirlpoolApiError:
            result = await client.request("GET", f"/api/v1/account/favorites/{said}")
        return _response(result)

    async def delete_favorite(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        said = _service_said(hass, call)
        favorite_id = str(call.data["favorite_id"])
        return _response(await client.request("DELETE", f"/api/v3/account/favorites/{said}/{favorite_id}"))

    async def get_messages(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        return _response(await client.request("GET", f"/api/v1/users/{await _user_id(client)}/messages"))

    async def get_message(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", f"/api/v1/messages/{call.data['message_id']}"))

    async def dismiss_message(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        user_id = await _user_id(client)
        return _response(await client.request("DELETE", f"/api/v1/users/{user_id}/messages/{call.data['message_id']}"))

    async def get_accessories(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", "/api/v1/accessory"))

    async def get_accessory_status(call: ServiceCall) -> dict[str, Any]:
        serial_number = str(call.data["serial_number"])
        return _response(await _first_client(hass).request("GET", f"/api/v1/accessory/{serial_number}"))

    async def get_accessory_cycle_history(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", "/api/v1/accessory/cycle-history", params=dict(call.data.get("params") or {}) or None))

    async def get_accessory_ota_status(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", "/api/v1/accessory/ota"))

    async def live_collect(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", f"/api/v1/live/collect/{_service_said(hass, call)}"))

    async def get_ac_filter_status(call: ServiceCall) -> dict[str, Any]:
        return _response(await _first_client(hass).request("GET", f"/api/v1/ac/getACFilterStatus/{_service_said(hass, call)}"))

    async def reset_ac_filter(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        said = _service_said(hass, call)
        body = dict(call.data.get("body") or {})
        body.setdefault("said", said)
        body.setdefault("saId", said)
        result = await client.request("POST", "/api/v1/ac/resetACFilter", json=body)
        await _first_coordinator(hass).async_request_refresh()
        return _response(result)

    _register(hass, "call_api", call_api, vol.Schema({vol.Required("path"): str, vol.Optional("method", default="GET"): str, vol.Optional("body"): object, vol.Optional("params"): object, vol.Optional("auth", default=True): bool}))
    _register(hass, "send_appliance_command", send_command, _said_schema({vol.Optional("command", default="setAttributes"): str, vol.Optional("attributes"): object, vol.Optional("raw"): object}))
    _register(hass, "set_cavity_light", set_cavity_light, _said_schema({vol.Required("enabled"): bool, vol.Optional("on"): bool, vol.Optional("cavity"): str}))
    _register(hass, "publish_thing_command", publish_thing_command, _said_schema({vol.Optional("command", default="getState"): str, vol.Optional("payload"): object}))
    _register(hass, "set_attributes", set_attributes, _said_schema({vol.Required("attributes"): dict}))
    _register(hass, "set_oven_cook", set_oven_cook, _said_schema({vol.Required("temperature"): vol.Coerce(float), vol.Optional("mode", default="bake"): vol.In(["bake", "convect_bake", "convection_bake", "broil", "convect_broil", "convection_broil", "convect_roast", "convection_roast", "keep_warm", "air_fry"]), vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"]), vol.Optional("cook_time_minutes"): vol.Coerce(float), vol.Optional("delay_time_minutes"): vol.Coerce(float), vol.Optional("complete_action", default="turn_off"): vol.In(["turn_off", "keep_warm", "stay_on"])}))
    _register(hass, "set_oven_frozen_bake", set_oven_frozen_bake, _said_schema({vol.Required("food"): vol.In(["pizza", "pie", "meals", "fries", "nuggets", "lasagna"]), vol.Required("temperature"): vol.Coerce(float), vol.Required("cook_time_minutes"): vol.Coerce(float), vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"]), vol.Optional("complete_action", default="turn_off"): vol.In(["turn_off", "keep_warm", "stay_on"])}))
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
    _register(hass, "update_appliances", update_appliances, vol.Schema({vol.Optional("body"): object}))
    _register(hass, "get_ddm_content", get_ddm_content, vol.Schema({vol.Required("ddm_key"): str}))
    _register(hass, "appliance_function", appliance_function, _said_schema({vol.Required("function"): vol.In(list(APPLIANCE_FUNCTIONS)), vol.Optional("body"): object, vol.Optional("path_values"): object}))
    _register(hass, "get_cycle_history", get_cycle_history, _history_schema())
    _register(hass, "get_fault_history", get_fault_history, _history_schema())
    _register(hass, "get_favorites", get_favorites, _said_schema())
    _register(hass, "delete_favorite", delete_favorite, _said_schema({vol.Required("favorite_id"): str}))
    _register(hass, "get_messages", get_messages, vol.Schema({}))
    _register(hass, "get_message", get_message, vol.Schema({vol.Required("message_id"): str}))
    _register(hass, "dismiss_message", dismiss_message, vol.Schema({vol.Required("message_id"): str}))
    _register(hass, "get_accessories", get_accessories, vol.Schema({}))
    _register(hass, "get_accessory_status", get_accessory_status, vol.Schema({vol.Required("serial_number"): str}))
    _register(hass, "get_accessory_cycle_history", get_accessory_cycle_history, vol.Schema({vol.Optional("params"): dict}))
    _register(hass, "get_accessory_ota_status", get_accessory_ota_status, vol.Schema({}))
    _register(hass, "live_collect", live_collect, _said_schema())
    _register(hass, "get_ac_filter_status", get_ac_filter_status, _said_schema())
    _register(hass, "reset_ac_filter", reset_ac_filter, _said_schema({vol.Optional("body"): dict}))


def unregister_services(hass: HomeAssistant) -> None:
    """Unregister Whirlpool Appliances services."""
    for service in (
        "call_api", "send_appliance_command", "set_cavity_light", "publish_thing_command", "set_attributes", "set_oven_cook", "set_oven_frozen_bake", "stop_oven_cavity", "stop_microwave", "set_quiet_mode", "set_remote_enable", "set_kitchen_timer", "stop_kitchen_timer", "check_firmware_update", "sync_time", "set_time_auto_update", "set_timezone", "refresh", "refresh_ddm_capabilities", "update_appliances", "get_ddm_content", "appliance_function", "get_cycle_history", "get_fault_history", "get_favorites", "delete_favorite", "get_messages", "get_message", "dismiss_message", "get_accessories", "get_accessory_status", "get_accessory_cycle_history", "get_accessory_ota_status", "live_collect", "get_ac_filter_status", "reset_ac_filter",
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _register(hass: HomeAssistant, service: str, handler, schema: vol.Schema | None) -> None:
    kwargs: dict[str, Any] = {"supports_response": SupportsResponse.OPTIONAL}
    if schema is not None:
        kwargs["schema"] = schema
    hass.services.async_register(DOMAIN, service, handler, **kwargs)


def _said_schema(extra: dict[Any, Any] | None = None) -> vol.Schema:
    data: dict[Any, Any] = {vol.Optional("appliance_device"): str, vol.Optional("said"): str}
    data.update(extra or {})
    return vol.Schema(data)


def _bool_schema() -> vol.Schema:
    return _said_schema({vol.Required("enabled"): bool, vol.Optional("on"): bool})


def _history_schema() -> vol.Schema:
    return _said_schema({vol.Optional("limit"): vol.Coerce(int), vol.Optional("params"): dict})


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


def _history_params(hass: HomeAssistant, call: ServiceCall) -> dict[str, Any]:
    params = dict(call.data.get("params") or {})
    if call.data.get("said") or call.data.get("appliance_device"):
        said = _service_said(hass, call)
        params.setdefault("said", said)
        params.setdefault("saId", said)
    if "limit" in call.data:
        limit = int(call.data["limit"])
        params.setdefault("limit", limit)
        params.setdefault("maxRecordCount", limit)
    return params


async def _user_id(client: WhirlpoolCloudClient) -> str:
    if not client.user_id:
        await client._populate_user_details()  # noqa: SLF001 - existing profile helper
    if not client.user_id:
        raise HomeAssistantError("Whirlpool account user ID is unavailable")
    return str(client.user_id)


def _service_result(result: Any) -> dict[str, Any]:
    if result is False:
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="request_failed")
    if isinstance(result, dict):
        status = str(result.get("status", "")).strip().lower()
        message = str(result.get("message", "")).strip().lower()
        if status in {"error", "failed", "fail", "02", "2", "nack"} or "negative acknow" in message:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="request_failed")
    return _response(result)


def _response(result: Any) -> dict[str, Any]:
    return {"result": result}
