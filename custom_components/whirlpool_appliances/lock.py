"""Lock entities for Whirlpool Appliances integration."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key, is_cooking_appliance


class WhirlpoolControlLock(WhirlpoolApkEntity, LockEntity):
    """Control lock entity for cooking appliances."""

    entity_description = LockEntityDescription(
        key="control_lock",
        translation_key="control_lock",
        icon="mdi:lock",
    )

    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "control_lock")
        self._attr_name = entity_name_from_key("control_lock", appliance)

    @property
    def icon(self) -> str | None:
        return "mdi:lock" if self.is_locked else "mdi:lock-open-variant"

    @property
    def is_locked(self) -> bool | None:
        """Return True when the appliance reports control lock enabled.

        Whirlpool's Sys_OperationSetControlLock uses:
          0 = unlocked
          1 = locked

        Use both attr_value and find_key because different flattened payloads can
        expose legacy attributes in slightly different shapes. Never return None
        for an explicit 0 value; Home Assistant needs False to display Unlocked.
        """
        raw = attr_value(self.flat_status, "Sys_OperationSetControlLock")
        if raw is None:
            raw = find_key(self.flat_status, ("Sys_OperationSetControlLock",))

        if raw is None:
            return None

        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"0", "false", "off", "no", "unlocked", "unlock"}:
                return False
            if normalized in {"1", "true", "on", "yes", "locked", "lock"}:
                return True
            return None

        return bool(raw)

    async def async_lock(self, **kwargs: Any) -> None:
        self._check_service_request(await self.client.set_oven_control_lock(self.said, True))
        await self.coordinator.async_request_refresh()

    async def async_unlock(self, **kwargs: Any) -> None:
        self._check_service_request(await self.client.set_oven_control_lock(self.said, False))
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WhirlpoolApkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[WhirlpoolControlLock] = []

    for appliance in (coordinator.data or {}).get("appliances", []):
        if appliance_said(appliance) and is_cooking_appliance(appliance):
            entities.append(WhirlpoolControlLock(coordinator, appliance))

    async_add_entities(entities)
