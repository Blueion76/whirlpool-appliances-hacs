"""Service registration for Whirlpool Appliances."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .api import WhirlpoolApiError, WhirlpoolCloudClient
from .const import DATA_CLIENT, DATA_COORDINATOR, DOMAIN


# Keep the public service surface small and Home Assistant-like.  More specific
# Whirlpool mobile-app endpoints are exposed through grouped actions instead of
# one service per endpoint.
SERVICE_NAMES = (
    "call_api",
    "send_appliance_command",
    "set_attributes",
    "oven_control",
    "appliance_option",
    "refresh",
    "history",
    "favorites",
    "messages",
    "feature",
)

OVEN_ACTIONS = {"set_cook", "set_frozen_bake", "stop"}
OPTION_ACTIONS = {
    "set_cavity_light",
    "set_quiet_mode",
    "set_remote_enable",
    "set_kitchen_timer",
    "stop_kitchen_timer",
    "check_firmware_update",
    "sync_time",
    "set_time_auto_update",
    "set_timezone",
    "get_ac_filter_status",
    "reset_ac_filter",
}
HISTORY_ACTIONS = {"cycle", "fault"}
FAVORITE_ACTIONS = {"get", "delete"}
MESSAGE_ACTIONS = {"list", "get", "dismiss"}
ACCESSORY_ACTIONS = {
    "get_accessories",
    "get_accessory_status",
    "get_accessory_cycle_history",
    "get_accessory_cycle",
    "get_accessory_cycle_lifecycle",
    "get_accessory_favorites",
    "save_accessory_cycle_favorite",
    "delete_accessory_favorite",
    "rename_accessory_favorite",
    "update_accessory_favorite_notes",
    "get_accessory_expert_cycle",
    "enable_accessory_range_extender",
    "get_accessory_ota_status",
}
FEATURE_ACTIONS = {
    "update_appliances",
    "get_ddm_content",
    *ACCESSORY_ACTIONS,
    "get_notification_subscriptions",
    "get_notification_device",
    "get_ts_ota_status",
    "get_ts_ota_descriptor_status",
    "get_ts_ota_descriptor",
    "get_appliance_documents",
    "get_thing_state",
    "live_collect",
    "get_video_capabilities",
}


def register_services(hass: HomeAssistant) -> None:
    """Register Whirlpool Appliances services."""
    if hass.services.has_service(DOMAIN, "call_api"):
        return

    async def call_api(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        path = call.data["path"]
        if not path.startswith("/") and not path.startswith(client.base_url):
            raise HomeAssistantError("Path must start with '/' or the configured Whirlpool base URL")
        return _response(
            await client.request(
                call.data.get("method", "GET"),
                path,
                json=call.data.get("body"),
                params=call.data.get("params"),
                auth=call.data.get("auth", True),
            )
        )

    async def send_appliance_command(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.send_appliance_command(
            _service_said(hass, call),
            call.data.get("command", "setAttributes"),
            call.data.get("attributes"),
            call.data.get("raw"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_attributes(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        result = await client.send_attributes(_service_said(hass, call), call.data["attributes"])
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def oven_control(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        said = _service_said(hass, call)
        action = str(call.data["action"])
        if action == "set_cook":
            result = await client.set_oven_cook(
                said,
                call.data["temperature"],
                call.data.get("mode", "bake"),
                call.data.get("cavity", "upper"),
                cook_time_seconds=_minutes_to_seconds(call.data.get("cook_time_minutes")),
                delay_time_seconds=_minutes_to_seconds(call.data.get("delay_time_minutes")),
                complete_action=call.data.get("complete_action", "turn_off"),
            )
        elif action == "set_frozen_bake":
            result = await client.set_oven_frozen_bake(
                said,
                call.data["food"],
                call.data["temperature"],
                int(round(float(call.data["cook_time_minutes"]) * 60)),
                call.data.get("cavity", "upper"),
                complete_action=call.data.get("complete_action", "turn_off"),
            )
        elif action == "stop":
            result = await client.stop_oven_cavity(said, call.data.get("cavity", "upper"))
        else:
            raise HomeAssistantError(f"Unsupported oven action: {action}")
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def appliance_option(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        said = _service_said(hass, call)
        action = str(call.data["action"])
        if action == "set_cavity_light":
            result = await client.set_cavity_light(said, _service_bool(call), call.data.get("cavity"))
        elif action == "set_quiet_mode":
            result = await client.set_quiet_mode(said, _service_bool(call))
        elif action == "set_remote_enable":
            result = await client.set_remote_enable(said, _service_bool(call))
        elif action == "set_kitchen_timer":
            result = await client.set_kitchen_timer(said, call.data["seconds"], call.data.get("timer", 1))
        elif action == "stop_kitchen_timer":
            result = await client.stop_kitchen_timer(said, call.data.get("timer", 1))
        elif action == "check_firmware_update":
            result = await client.check_firmware_update(said)
        elif action == "sync_time":
            result = await client.sync_appliance_time(said, call.data.get("timezone"))
        elif action == "set_time_auto_update":
            result = await client.set_time_auto_update(said, _service_bool(call), call.data.get("timezone"))
        elif action == "set_timezone":
            timezone = str(call.data["timezone"])
            result = await client.send_attributes(said, {"TimeZoneId": timezone, "TimezoneId": timezone, "XCat_TimeZoneId": timezone})
        elif action == "get_ac_filter_status":
            result = await client.request("GET", f"/api/v1/ac/getACFilterStatus/{said}")
        elif action == "reset_ac_filter":
            body = dict(call.data.get("body") or {})
            body.setdefault("said", said)
            body.setdefault("saId", said)
            result = await client.request("POST", "/api/v1/ac/resetACFilter", json=body)
        else:
            raise HomeAssistantError(f"Unsupported option action: {action}")
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def refresh(call: ServiceCall) -> dict[str, Any] | None:
        action = str(call.data.get("action", "status"))
        coordinator = _first_coordinator(hass)
        if action == "status":
            await coordinator.async_request_refresh()
            return _response({"status": "refreshed"})
        if action == "ddm_capabilities":
            await coordinator.async_fetch_ddm_capabilities(force=True)
            data = dict(coordinator.data or {})
            data["ddm_capabilities"] = coordinator._ddm_capabilities
            data["ddm_errors"] = coordinator._ddm_errors
            coordinator.async_set_updated_data(data)
            return _response({"ddm_keys": list(coordinator._ddm_capabilities), "errors": coordinator._ddm_errors})
        raise HomeAssistantError(f"Unsupported refresh action: {action}")

    async def history(call: ServiceCall) -> dict[str, Any]:
        action = str(call.data["action"])
        path = "/api/v1/history/cycle" if action == "cycle" else "/api/v1/history/faultCode"
        if action not in HISTORY_ACTIONS:
            raise HomeAssistantError(f"Unsupported history action: {action}")
        return _response(await _first_client(hass).request("GET", path, params=_history_params(hass, call) or None))

    async def favorites(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        said = _service_said(hass, call)
        action = str(call.data["action"])
        if action == "get":
            try:
                result = await client.request("GET", f"/api/v2/account/favorites/{said}")
            except WhirlpoolApiError:
                result = await client.request("GET", f"/api/v1/account/favorites/{said}")
        elif action == "delete":
            result = await client.request("DELETE", f"/api/v3/account/favorites/{said}/{call.data['favorite_id']}")
        else:
            raise HomeAssistantError(f"Unsupported favorites action: {action}")
        return _response(result)

    async def messages(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        action = str(call.data["action"])
        if action == "list":
            result = await client.request("GET", f"/api/v1/users/{await _user_id(client)}/messages")
        elif action == "get":
            result = await client.request("GET", f"/api/v1/messages/{call.data['message_id']}")
        elif action == "dismiss":
            result = await client.request("DELETE", f"/api/v1/users/{await _user_id(client)}/messages/{call.data['message_id']}")
        else:
            raise HomeAssistantError(f"Unsupported messages action: {action}")
        return _response(result)

    async def feature(call: ServiceCall) -> dict[str, Any]:
        result = await _feature_response(hass, call)
        return _response(result)

    _register(hass, "call_api", call_api, vol.Schema({vol.Required("path"): str, vol.Optional("method", default="GET"): str, vol.Optional("body"): object, vol.Optional("params"): object, vol.Optional("auth", default=True): bool}))
    _register(hass, "send_appliance_command", send_appliance_command, _said_schema({vol.Optional("command", default="setAttributes"): str, vol.Optional("attributes"): object, vol.Optional("raw"): object}))
    _register(hass, "set_attributes", set_attributes, _said_schema({vol.Required("attributes"): dict}))
    _register(hass, "oven_control", oven_control, _said_schema({vol.Required("action"): vol.In(sorted(OVEN_ACTIONS)), vol.Optional("temperature"): vol.Coerce(float), vol.Optional("mode", default="bake"): str, vol.Optional("food"): str, vol.Optional("cook_time_minutes"): vol.Coerce(float), vol.Optional("delay_time_minutes"): vol.Coerce(float), vol.Optional("complete_action", default="turn_off"): str, vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"])}))
    _register(hass, "appliance_option", appliance_option, _said_schema({vol.Required("action"): vol.In(sorted(OPTION_ACTIONS)), vol.Optional("enabled"): bool, vol.Optional("on"): bool, vol.Optional("cavity"): str, vol.Optional("seconds"): vol.Coerce(int), vol.Optional("timer", default=1): vol.Coerce(int), vol.Optional("timezone"): str, vol.Optional("body"): dict}))
    _register(hass, "refresh", refresh, vol.Schema({vol.Optional("action", default="status"): vol.In(["status", "ddm_capabilities"])}))
    _register(hass, "history", history, _said_schema({vol.Required("action"): vol.In(sorted(HISTORY_ACTIONS)), vol.Optional("limit"): vol.Coerce(int), vol.Optional("params"): dict}))
    _register(hass, "favorites", favorites, _said_schema({vol.Required("action"): vol.In(sorted(FAVORITE_ACTIONS)), vol.Optional("favorite_id"): str}))
    _register(hass, "messages", messages, vol.Schema({vol.Required("action"): vol.In(sorted(MESSAGE_ACTIONS)), vol.Optional("message_id"): str}))
    _register(hass, "feature", feature, _said_schema({vol.Required("action"): vol.In(sorted(FEATURE_ACTIONS)), vol.Optional("params"): dict, vol.Optional("body"): object, vol.Optional("ddm_key"): str, vol.Optional("serial_number"): str, vol.Optional("cycle_id"): str, vol.Optional("favorite_id"): str, vol.Optional("favorite_name"): str, vol.Optional("notes"): str, vol.Optional("device_id"): str}))


def unregister_services(hass: HomeAssistant) -> None:
    """Unregister Whirlpool Appliances services."""
    for service in SERVICE_NAMES:
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def _feature_response(hass: HomeAssistant, call: ServiceCall) -> Any:
    client = _first_client(hass)
    action = str(call.data["action"])
    params = dict(call.data.get("params") or {}) or None
    body = call.data.get("body")

    if action in ACCESSORY_ACTIONS and not _has_accessories(client):
        return {
            "status": "skipped",
            "action": action,
            "reason": "No accessories are reported by this Whirlpool account token.",
            "accessories": [],
        }

    accessory_headers = _accessory_headers(client)

    if action == "update_appliances":
        result = await client.request("POST", "/api/v2/updateAppliances", json=body)
        await _first_coordinator(hass).async_request_refresh()
        return result
    if action == "get_ddm_content":
        return await client.request("GET", f"/api/v1/contents/all/{call.data['ddm_key']}")
    if action == "get_accessories":
        return await client.request("GET", "/api/v1/accessory", extra_headers=accessory_headers)
    if action == "get_accessory_status":
        return await client.request("GET", f"/api/v1/accessory/{call.data['serial_number']}", extra_headers=accessory_headers)
    if action == "get_accessory_cycle_history":
        return await client.request("GET", "/api/v1/accessory/cycle-history", params=params, extra_headers=accessory_headers)
    if action == "get_accessory_cycle":
        return await client.request("GET", f"/api/v1/accessory/cycle-history/{call.data['cycle_id']}", extra_headers=accessory_headers)
    if action == "get_accessory_cycle_lifecycle":
        return await client.request("GET", "/api/v1/accessory/cycle-history/lifecycle/", params=params, extra_headers=accessory_headers)
    if action == "get_accessory_favorites":
        return await client.request("GET", "/api/v1/accessory/cycle-history/favorites", params=params, extra_headers=accessory_headers)
    if action == "save_accessory_cycle_favorite":
        return await client.request("POST", f"/api/v1/accessory/cycle-history/favorites/{call.data['cycle_id']}", json=body, extra_headers=accessory_headers)
    if action == "delete_accessory_favorite":
        return await client.request("DELETE", f"/api/v1/accessory/cycle-history/favorites/delete/{call.data['favorite_id']}", extra_headers=accessory_headers)
    if action == "rename_accessory_favorite":
        payload = dict(body or {})
        if call.data.get("favorite_name") is not None:
            payload.setdefault("name", call.data["favorite_name"])
        return await client.request("PUT", f"/api/v1/accessory/cycle-history/favorites/name/{call.data['cycle_id']}", json=payload, extra_headers=accessory_headers)
    if action == "update_accessory_favorite_notes":
        payload = dict(body or {})
        if call.data.get("notes") is not None:
            payload.setdefault("notes", call.data["notes"])
        return await client.request("PUT", f"/api/v1/accessory/cycle-history/favorites/notes/{call.data['favorite_id']}", json=payload, extra_headers=accessory_headers)
    if action == "get_accessory_expert_cycle":
        return await client.request("GET", f"/api/v1/accessory/expert/cycle/{call.data['cycle_id']}", extra_headers=accessory_headers)
    if action == "enable_accessory_range_extender":
        return await client.request("POST", f"/api/v1/accessory/rangeextender/enable/{call.data['cycle_id']}", json=body, extra_headers=accessory_headers)
    if action == "get_accessory_ota_status":
        return await client.request("GET", "/api/v1/accessory/ota", extra_headers=accessory_headers)
    if action == "get_notification_subscriptions":
        return await client.request("GET", "/api/v2/notifications/subscriptions/multi", params=params)
    if action == "get_notification_device":
        return await client.request("GET", f"/api/v2/user/notification/device/{call.data['device_id']}", params=params)
    if action == "get_ts_ota_status":
        return await client.request("GET", f"/api/v1/ts/ota/status/{_service_said(hass, call)}")
    if action == "get_ts_ota_descriptor_status":
        return await client.request("GET", f"/api/v1/ts/ota/descriptor/status/{_service_said(hass, call)}")
    if action == "get_ts_ota_descriptor":
        return await client.request("GET", "/api/v2/ts/ota/descriptor", params=params)
    if action == "get_appliance_documents":
        ddm_key = call.data.get("ddm_key")
        if ddm_key:
            return await client.request("GET", f"/api/v1/contents/all/{ddm_key}", params={"contentType": "documents"})
        return await client.request("GET", "/api/v1/contents/all", params=params)
    if action == "get_thing_state":
        return await _first_coordinator(hass).async_publish_thing_command(_service_said(hass, call), "getState")
    if action in {"live_collect", "get_video_capabilities"}:
        result = await client.request("GET", f"/api/v1/live/collect/{_service_said(hass, call)}")
        if action == "get_video_capabilities":
            return {"video_capability_hints": _video_hints(result), "raw": result}
        return result
    raise HomeAssistantError(f"Unsupported feature action: {action}")


def _has_accessories(client: WhirlpoolCloudClient) -> bool:
    """Return true if the account token reports at least one accessory."""
    auth_payload = getattr(client, "auth_payload", {}) or {}
    accessories = auth_payload.get("accessories")
    return isinstance(accessories, list) and len(accessories) > 0


def _accessory_headers(client: WhirlpoolCloudClient) -> dict[str, str]:
    """Return mobile-app headers required by accessory endpoints."""
    region = str(client.region or "US").upper()
    brand = str(client.brand)
    return {
        "WP-CLIENT-BRAND": brand,
        "WP-CLIENT-COUNTRY": region,
        "WP-CLIENT-REGION": region,
        "x-client-brand": brand,
        "x-client-country": region,
        "x-client-region": region,
    }


def _video_hints(payload: Any) -> dict[str, Any]:
    text = str(payload).lower()
    return {
        "mentions_camera": "camera" in text,
        "mentions_kinesis": "kinesis" in text,
        "mentions_webrtc": "webrtc" in text,
        "mentions_stream": "stream" in text,
        "mentions_ice_servers": "iceserver" in text or "ice_server" in text or "ice server" in text,
    }


def _register(hass: HomeAssistant, service: str, handler, schema: vol.Schema | None) -> None:
    kwargs: dict[str, Any] = {"supports_response": SupportsResponse.OPTIONAL}
    if schema is not None:
        kwargs["schema"] = schema
    hass.services.async_register(DOMAIN, service, handler, **kwargs)


def _said_schema(extra: dict[Any, Any] | None = None) -> vol.Schema:
    data: dict[Any, Any] = {vol.Optional("appliance_device"): str, vol.Optional("said"): str}
    data.update(extra or {})
    return vol.Schema(data)


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
