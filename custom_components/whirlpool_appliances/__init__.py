"""Whirlpool Appliances Home Assistant integration."""
from __future__ import annotations

import logging
from typing import Any, TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_USERNAME, Platform, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    WhirlpoolAccountLockedError,
    WhirlpoolApiError,
    WhirlpoolAuthError,
    WhirlpoolCloudClient,
)
from .const import (
    CONF_BRAND,
    CONF_SCAN_INTERVAL,
    DATA_CLIENT,
    DATA_COORDINATOR,
    DEFAULT_BRAND,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import WhirlpoolApkCoordinator
from .helpers.logging import summarize
from .services import register_services, unregister_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.LOCK,
    Platform.SELECT,
    Platform.TIME,
    Platform.UPDATE,
]

WhirlpoolApkConfigEntry: TypeAlias = ConfigEntry[WhirlpoolApkCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry) -> bool:
    """Set up Whirlpool Appliances from a config entry."""
    client = await _login_client(hass, entry)
    coordinator = WhirlpoolApkCoordinator(
        hass,
        client,
        _entry_option(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await coordinator.async_config_entry_first_refresh()
    if not (coordinator.data or {}).get("appliances"):
        _LOGGER.warning("Whirlpool setup did not find any appliances")
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="appliances_fetch_failed",
        )

    _LOGGER.debug(
        "Whirlpool setup discovered appliances: %s",
        summarize((coordinator.data or {}).get("appliances")),
    )
    await coordinator.async_start_push()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        DATA_CLIENT: client,
        DATA_COORDINATOR: coordinator,
    }

    _patch_temperature_unit_preference()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    await entry.runtime_data.async_shutdown()
    entries = hass.data.get(DOMAIN, {})
    entries.pop(entry.entry_id, None)
    if not entries:
        hass.data.pop(DOMAIN, None)
        unregister_services(hass)
    return True


async def _login_client(
    hass: HomeAssistant,
    entry: WhirlpoolApkConfigEntry,
) -> WhirlpoolCloudClient:
    """Create and authenticate the Whirlpool cloud client."""
    client = WhirlpoolCloudClient(
        async_get_clientsession(hass),
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        region=entry.data.get(CONF_REGION, DEFAULT_REGION),
        brand=entry.data.get(CONF_BRAND, DEFAULT_BRAND),
    )

    try:
        await client.login()
    except WhirlpoolAccountLockedError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="account_locked",
        ) from err
    except WhirlpoolAuthError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="invalid_auth",
        ) from err
    except WhirlpoolApiError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
        ) from err

    return client


async def _async_update_listener(
    hass: HomeAssistant,
    entry: WhirlpoolApkConfigEntry,
) -> None:
    """Reload config entry when options are updated."""
    await hass.config_entries.async_reload(entry.entry_id)


def _entry_option(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read option value with fallback to config entry data."""
    return entry.options.get(key, entry.data.get(key, default))


def _patch_temperature_unit_preference() -> None:
    """Make Whirlpool temperature entities follow Home Assistant's unit system.

    Whirlpool legacy payloads report cooking temperatures in Celsius/tenths of
    Celsius. Entity/native display should still follow the user's HA unit system
    instead of the Whirlpool account region.
    """
    from .entity import WhirlpoolApkEntity, temperature_unit_for_region

    def _preferred_temperature_unit(entity: WhirlpoolApkEntity) -> UnitOfTemperature:
        try:
            unit = entity.coordinator.hass.config.units.temperature_unit
        except Exception:  # noqa: BLE001 - fall back to previous region behavior
            unit = None
        if unit in (UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT):
            return unit
        region = getattr(entity.coordinator.client, "region", None)
        return temperature_unit_for_region(region)

    WhirlpoolApkEntity.temperature_unit = property(_preferred_temperature_unit)
