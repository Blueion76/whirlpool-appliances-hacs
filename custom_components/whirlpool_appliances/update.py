"""Update entities for Whirlpool Appliances integration."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key


def _version_from_status(flat: Mapping[str, Any]) -> str | None:
    """Return the best displayed appliance/firmware version from raw status."""
    for attr in (
        "ISP_WIFI_VERSION",
        "ProjectReleaseNumber",
        "ApplianceVersionNumber",
        "version",
    ):
        value = attr_value(flat, attr)
        if value not in (None, "", "0", 0):
            return str(value)

    value = find_key(flat, ("firmwareVersion", "softwareVersion", "fwVersion", "version"))
    if value not in (None, "", "0", 0):
        return str(value)
    return None


class WhirlpoolFirmwareUpdateEntity(WhirlpoolApkEntity, UpdateEntity):
    """Firmware update availability from Whirlpool cloud."""

    _attr_translation_key = "firmware_update"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_supported_features = UpdateEntityFeature(0)

    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "firmware_update")
        self._attr_name = entity_name_from_key("firmware_update", appliance)
        self._update_available: bool | None = None
        self._latest_version: str | None = None

    @property
    def installed_version(self) -> str | None:
        return _version_from_status(self.flat_status)

    @property
    def latest_version(self) -> str | None:
        if self._latest_version:
            return self._latest_version
        if self._update_available:
            return "Available"
        return self.installed_version

    @property
    def title(self) -> str:
        return "Whirlpool firmware"

    @property
    def release_summary(self) -> str | None:
        if self._update_available:
            return "Whirlpool reports a firmware update is available."
        return None

    async def async_update(self) -> None:
        payload = await self.client.check_firmware_update(self.said)
        self._update_available = None
        self._latest_version = None

        if not isinstance(payload, Mapping):
            return

        available = payload.get("FirmwareUpdateAvailable")
        if isinstance(available, str):
            self._update_available = available.strip().lower() in {"1", "true", "yes", "available"}
        elif available is not None:
            self._update_available = bool(available)

        for key in ("LatestVersion", "latestVersion", "FirmwareVersion", "firmwareVersion", "version"):
            value = payload.get(key)
            if value not in (None, "", "0", 0):
                self._latest_version = str(value)
                break


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WhirlpoolApkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[UpdateEntity] = []

    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        entities.append(WhirlpoolFirmwareUpdateEntity(coordinator, appliance))

    async_add_entities(entities)
