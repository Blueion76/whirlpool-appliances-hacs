"""Switch entities for common Whirlpool controls."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key, is_cooking_appliance, microwave_exists


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower() in ("1", "true", "on", "yes")
    return bool(value)


@dataclass(frozen=True, kw_only=True)
class WhirlpoolSwitchDescription(SwitchEntityDescription):
    value_fn: Callable[[Mapping[str, Any]], bool | None]
    set_fn: Callable[[Any, str, bool], Awaitable[Any]]
    cooking_only: bool = False
    non_cooking_only: bool = False
    microwave_only: bool = False


async def _set_power(client, said: str, on: bool):
    return await client.set_power(said, on)


async def _set_control_lock(client, said: str, on: bool):
    return await client.set_oven_control_lock(said, on)


async def _set_sabbath(client, said: str, on: bool):
    return await client.set_oven_sabbath_mode(said, on)


async def _set_microwave_turntable(client, said: str, on: bool):
    return await client.set_microwave_turntable(said, on)


SWITCHES = (
    WhirlpoolSwitchDescription(key="power", translation_key="power", entity_registry_enabled_default=False, value_fn=lambda flat: _to_bool(find_key(flat, ("powerOn", "power", "isOn"))), set_fn=_set_power, non_cooking_only=True),
    WhirlpoolSwitchDescription(key="control_lock", translation_key="control_lock", value_fn=lambda flat: _to_bool(attr_value(flat, "Sys_OperationSetControlLock")), set_fn=_set_control_lock, cooking_only=True),
    WhirlpoolSwitchDescription(key="sabbath_mode", translation_key="sabbath_mode", entity_registry_enabled_default=False, value_fn=lambda flat: _to_bool(attr_value(flat, "Sys_OperationSetSabbathModeEnabled")), set_fn=_set_sabbath, cooking_only=True),
    WhirlpoolSwitchDescription(key="microwave_turntable", translation_key="microwave_turntable", value_fn=lambda flat: _to_bool(attr_value(flat, "Mwo_CycleSetTurntable")), set_fn=_set_microwave_turntable, cooking_only=True, microwave_only=True),
)


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        cooking = is_cooking_appliance(appliance)
        flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
        has_mwo = microwave_exists(flat)
        for desc in SWITCHES:
            # Do not expose the generic Power switch for ovens / microwave combos.
            # Whirlpool legacy cooking products cannot be safely "powered on" without
            # an explicit mode and target temperature, so the generic switch caused
            # a Home Assistant error when users tapped it. Use the climate entity or
            # set_oven_cook service to start cooking and the Stop Oven/Microwave
            # buttons to cancel.
            if desc.cooking_only and not cooking:
                continue
            if desc.non_cooking_only and cooking:
                continue
            if desc.microwave_only and not has_mwo:
                continue
            entities.append(WhirlpoolApkSwitch(coordinator, appliance, desc))
    async_add_entities(entities)


class WhirlpoolApkSwitch(WhirlpoolApkEntity, SwitchEntity):
    entity_description: WhirlpoolSwitchDescription
    def __init__(self, coordinator, appliance: Mapping[str, object], description: WhirlpoolSwitchDescription) -> None:
        super().__init__(coordinator, appliance, description.key)
        self.entity_description = description
        self._attr_name = entity_name_from_key(description.translation_key or description.key, appliance)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.flat_status)

    async def async_turn_on(self, **kwargs) -> None:
        self._check_service_request(await self.entity_description.set_fn(self.client, self.said, True))
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self._check_service_request(await self.entity_description.set_fn(self.client, self.said, False))
        await self.coordinator.async_request_refresh()
