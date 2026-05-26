"""Whirlpool Appliances-derived Home Assistant integration."""
from __future__ import annotations

import logging
from typing import Any, TypeAlias

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WhirlpoolAccountLockedError, WhirlpoolApiError, WhirlpoolAuthError, WhirlpoolCloudClient
from .api_spec import APPLIANCE_FUNCTIONS, DISCOVERED_API_PATHS
from .const import (
    CONF_BRAND,
    CONF_ENABLE_CONTROL_ENTITIES,
    CONF_EXPOSE_RAW_SENSORS,
    CONF_SCAN_INTERVAL,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_BRAND,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import WhirlpoolApkCoordinator
from .logging_utils import summarize

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.LOCK,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.UPDATE,
]

WhirlpoolApkConfigEntry: TypeAlias = ConfigEntry[WhirlpoolApkCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry) -> bool:
    """Set up from config entry."""
    session = async_get_clientsession(hass)
    client = WhirlpoolCloudClient(
        session,
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        region=entry.data.get(CONF_REGION, DEFAULT_REGION),
        brand=entry.data.get(CONF_BRAND, DEFAULT_BRAND),
    )
    try:
        await client.login()
    except WhirlpoolAccountLockedError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="account_locked"
        ) from err
    except WhirlpoolAuthError as err:
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth") from err
    except WhirlpoolApiError as err:
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="cannot_connect") from err

    coordinator = WhirlpoolApkCoordinator(hass, client, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    _LOGGER.debug(
        "Setting up Whirlpool Appliances entry: entry_id=%s region=%s brand=%s scan_interval=%s",
        entry.entry_id,
        entry.data.get(CONF_REGION, DEFAULT_REGION),
        entry.data.get(CONF_BRAND, DEFAULT_BRAND),
        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    if not (coordinator.data or {}).get("appliances"):
        _LOGGER.warning("Whirlpool setup did not find any appliances")
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="appliances_fetch_failed")
    _LOGGER.debug("Whirlpool setup discovered appliances: %s", summarize((coordinator.data or {}).get("appliances")))
    await coordinator.async_start_push()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {DATA_CLIENT: client, DATA_COORDINATOR: coordinator}

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_shutdown()
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


def _first_client(hass: HomeAssistant) -> WhirlpoolCloudClient:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("No Whirlpool Appliances config entry is loaded")
    return next(iter(entries.values()))[DATA_CLIENT]


def _first_coordinator(hass: HomeAssistant) -> WhirlpoolApkCoordinator:
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise HomeAssistantError("No Whirlpool Appliances config entry is loaded")
    return next(iter(entries.values()))[DATA_COORDINATOR]


def _appliance_said_from_device(hass: HomeAssistant, device_id: str) -> str | None:
    """Resolve a Whirlpool SAID from a selected Home Assistant device."""
    registry = dr.async_get(hass)
    device = registry.async_get(device_id)
    if not device:
        return None

    # WhirlpoolApkEntity creates devices with identifiers={(DOMAIN, said)}.
    for domain, identifier in device.identifiers:
        if domain == DOMAIN and identifier:
            return str(identifier)
    return None


def _service_said(hass: HomeAssistant, call: ServiceCall) -> str:
    """Return SAID from explicit SAID or selected Whirlpool device."""
    said = call.data.get("said")
    if said:
        return str(said)

    device_id = call.data.get("appliance_device")
    if device_id:
        if isinstance(device_id, list):
            device_id = device_id[0]
        resolved = _appliance_said_from_device(hass, str(device_id))
        if resolved:
            return resolved

    raise HomeAssistantError("Select a Whirlpool appliance device or enter a SAID")


def _service_result(result: Any) -> dict[str, Any]:
    if result is False:
        _LOGGER.warning("Whirlpool service command failed with boolean False")
        raise HomeAssistantError(translation_domain=DOMAIN, translation_key="request_failed")
    if isinstance(result, dict):
        status = str(result.get("status", "")).strip().lower()
        message = str(result.get("message", "")).strip().lower()
        if status in {"error", "failed", "fail", "02", "2", "nack"} or "negative acknow" in message:
            _LOGGER.warning("Whirlpool service command rejected: result=%s", summarize(result))
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="request_failed")
    _LOGGER.debug("Whirlpool service command result accepted: %s", summarize(result))
    return {"result": result}

def _service_bool(call: ServiceCall, key: str = "enabled") -> bool:
    """Return a service boolean, accepting legacy 'on' for YAML/backward compatibility."""
    if key in call.data:
        return bool(call.data[key])
    if "on" in call.data:
        return bool(_service_bool(call))
    raise HomeAssistantError(f"Missing required boolean field: {key}")


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "call_api"):
        return

    async def call_api(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        path = call.data["path"]
        if not path.startswith("/") and not path.startswith(client.base_url):
            raise HomeAssistantError("Path must be a Whirlpool API path starting with '/' or the configured base URL")
        result = await client.request(
            call.data.get("method", "GET"),
            path,
            json=call.data.get("body"),
            params=call.data.get("params"),
        )
        return _service_result(result)

    async def send_command(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug("Whirlpool service send_appliance_command called: data=%s", summarize(dict(call.data)))
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
        result = await client.set_cavity_light(_service_said(hass, call), _service_bool(call), call.data.get("cavity"))
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def publish_thing_command(call: ServiceCall) -> dict[str, Any]:
        coordinator = _first_coordinator(hass)
        result = await coordinator.async_publish_thing_command(
            _service_said(hass, call),
            call.data.get("command", "getState"),
            call.data.get("payload"),
        )
        return _service_result(result)

    async def set_attributes(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug("Whirlpool service set_attributes called: data=%s", summarize(dict(call.data)))
        client = _first_client(hass)
        result = await client.send_attributes(_service_said(hass, call), call.data["attributes"])
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_oven_cook(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug("Whirlpool service set_oven_cook called: data=%s", summarize(dict(call.data)))
        client = _first_client(hass)
        result = await client.set_oven_cook(
            _service_said(hass, call),
            call.data["temperature"],
            call.data.get("mode", "bake"),
            call.data.get("cavity", "upper"),
            cook_time_seconds=(
                int(round(float(call.data["cook_time_minutes"]) * 60))
                if "cook_time_minutes" in call.data
                else None
            ),
            delay_time_seconds=(
                int(round(float(call.data["delay_time_minutes"]) * 60))
                if "delay_time_minutes" in call.data
                else None
            ),
            complete_action=call.data.get("complete_action", "turn_off"),
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    async def set_oven_frozen_bake(call: ServiceCall) -> dict[str, Any]:
        _LOGGER.debug("Whirlpool service set_oven_frozen_bake called: data=%s", summarize(dict(call.data)))
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
        client = _first_client(hass)
        result = await client.check_firmware_update(_service_said(hass, call))
        return _service_result(result)

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

    async def refresh(call: ServiceCall) -> None:
        await _first_coordinator(hass).async_request_refresh()

    async def refresh_ddm_capabilities(call: ServiceCall) -> dict[str, Any]:
        coordinator = _first_coordinator(hass)
        await coordinator.async_fetch_ddm_capabilities(force=True)
        data = dict(coordinator.data or {})
        data["ddm_capabilities"] = coordinator._ddm_capabilities
        data["ddm_errors"] = coordinator._ddm_errors
        coordinator.async_set_updated_data(data)
        return _service_result({
            "ddm_keys": list(coordinator._ddm_capabilities),
            "errors": coordinator._ddm_errors,
        })

    async def appliance_function(call: ServiceCall) -> dict[str, Any]:
        client = _first_client(hass)
        path_values = dict(call.data.get("path_values") or {})
        result = await client.call_function(
            call.data["function"],
            said=_service_said(hass, call),
            body=call.data.get("body"),
            **path_values,
        )
        await _first_coordinator(hass).async_request_refresh()
        return _service_result(result)

    hass.services.async_register(
        DOMAIN,
        "call_api",
        call_api,
        schema=vol.Schema({vol.Required("path"): str, vol.Optional("method", default="GET"): str, vol.Optional("body"): object, vol.Optional("params"): object}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "send_appliance_command",
        send_command,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Optional("command", default="setAttributes"): str, vol.Optional("attributes"): object, vol.Optional("raw"): object}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_cavity_light",
        set_cavity_light,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Required("enabled"): bool, vol.Optional("on"): bool, vol.Optional("cavity"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "publish_thing_command",
        publish_thing_command,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Optional("command", default="getState"): str, vol.Optional("payload"): object}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_attributes",
        set_attributes,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Required("attributes"): dict}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_oven_cook",
        set_oven_cook,
        schema=vol.Schema(
            {
                vol.Optional("appliance_device"): str, vol.Optional("said"): str,
                vol.Required("temperature"): vol.Coerce(float),
                vol.Optional("mode", default="bake"): vol.In([
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
                ]),
                vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"]),
                vol.Optional("cook_time_minutes"): vol.Coerce(float),
                vol.Optional("delay_time_minutes"): vol.Coerce(float),
                vol.Optional("complete_action", default="turn_off"): vol.In(["turn_off", "keep_warm", "stay_on"]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_oven_frozen_bake",
        set_oven_frozen_bake,
        schema=vol.Schema(
            {
                vol.Optional("appliance_device"): str, vol.Optional("said"): str,
                vol.Required("food"): vol.In(["pizza", "pie", "meals", "fries", "nuggets", "lasagna"]),
                vol.Required("temperature"): vol.Coerce(float),
                vol.Required("cook_time_minutes"): vol.Coerce(float),
                vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"]),
                vol.Optional("complete_action", default="turn_off"): vol.In(["turn_off", "keep_warm", "stay_on"]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "stop_oven_cavity",
        stop_oven_cavity,
        schema=vol.Schema(
            {vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Optional("cavity", default="upper"): vol.In(["upper", "lower"])}
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "stop_microwave",
        stop_microwave,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_quiet_mode",
        set_quiet_mode,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Required("enabled"): bool, vol.Optional("on"): bool}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_remote_enable",
        set_remote_enable,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Required("enabled"): bool, vol.Optional("on"): bool}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_kitchen_timer",
        set_kitchen_timer,
        schema=vol.Schema({
            vol.Optional("appliance_device"): str, vol.Optional("said"): str,
            vol.Required("seconds"): vol.Coerce(int),
            vol.Optional("timer", default=1): vol.Coerce(int),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "stop_kitchen_timer",
        stop_kitchen_timer,
        schema=vol.Schema({
            vol.Optional("appliance_device"): str, vol.Optional("said"): str,
            vol.Optional("timer", default=1): vol.Coerce(int),
        }),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "check_firmware_update",
        check_firmware_update,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "sync_time",
        sync_time,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Optional("timezone"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "set_time_auto_update",
        set_time_auto_update,
        schema=vol.Schema({vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Required("enabled"): bool, vol.Optional("timezone"): str}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, "refresh", refresh)
    hass.services.async_register(
        DOMAIN,
        "refresh_ddm_capabilities",
        refresh_ddm_capabilities,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "appliance_function",
        appliance_function,
        schema=vol.Schema({vol.Required("function"): vol.In(list(APPLIANCE_FUNCTIONS)), vol.Optional("appliance_device"): str, vol.Optional("said"): str, vol.Optional("body"): object, vol.Optional("path_values"): object}),
        supports_response=SupportsResponse.OPTIONAL,
    )
