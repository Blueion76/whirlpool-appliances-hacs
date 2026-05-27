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
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key, is_aircon_appliance, is_cooking_appliance, microwave_exists


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
    aircon_only: bool = False


async def _set_power(client, said: str, on: bool):
    return await client.set_power(said, on)



async def _set_remote_enable(client, said: str, on: bool):
    return await client.set_remote_enable(said, on)


async def _set_sabbath(client, said: str, on: bool):
    return await client.set_oven_sabbath_mode(said, on)


async def _set_quiet_mode(client, said: str, on: bool):
    return await client.set_quiet_mode(said, on)


async def _set_microwave_turntable(client, said: str, on: bool):
    return await client.set_microwave_turntable(said, on)


async def _set_aircon_quiet_mode(client, said: str, on: bool):
    return await client.set_aircon_quiet_mode(said, on)


async def _set_time_auto_update(client, said: str, on: bool):
    return await client.set_time_auto_update(said, on)


SWITCHES = (
    WhirlpoolSwitchDescription(key="power", translation_key="power", entity_registry_enabled_default=False, value_fn=lambda flat: _to_bool(find_key(flat, ("powerOn", "power", "isOn"))), set_fn=_set_power, non_cooking_only=True),
    WhirlpoolSwitchDescription(key="remote_enable", translation_key="remote_enable", icon="mdi:cloud-check-variant", value_fn=lambda flat: _to_bool(attr_value(flat, "XCat_RemoteSetRemoteControlEnable")), set_fn=_set_remote_enable, cooking_only=True),
    WhirlpoolSwitchDescription(key="sabbath_mode", translation_key="sabbath_mode", icon="mdi:candelabra-fire", entity_registry_enabled_default=False, value_fn=lambda flat: _to_bool(attr_value(flat, "Sys_OperationSetSabbathModeEnabled")), set_fn=_set_sabbath, cooking_only=True),
    WhirlpoolSwitchDescription(key="quiet_mode", translation_key="quiet_mode", icon="mdi:volume-high", value_fn=lambda flat: _to_bool(attr_value(flat, "Sys_OperationSetQuietModeEnabled")), set_fn=_set_quiet_mode, cooking_only=True),
    WhirlpoolSwitchDescription(key="ac_quiet_mode", translation_key="ac_quiet_mode", icon="mdi:volume-high", value_fn=lambda flat: _to_bool(attr_value(flat, "Sys_OpSetQuietModeEnabled") or find_key(flat, ("quietMode", "quiet", "acQuietMode"))), set_fn=_set_aircon_quiet_mode, aircon_only=True),
    WhirlpoolSwitchDescription(key="ac_turbo_mode", translation_key="ac_turbo_mode", icon="mdi:fan-plus", value_fn=lambda flat: _to_bool(attr_value(flat, "Cavity_OpSetTurboMode")), set_fn=_set_aircon_turbo_mode, aircon_only=True),
    WhirlpoolSwitchDescription(key="ac_eco_mode", translation_key="ac_eco_mode", icon="mdi:leaf", value_fn=lambda flat: _to_bool(attr_value(flat, "Sys_OpSetEcoModeEnabled")), set_fn=_set_aircon_eco_mode, aircon_only=True),
    WhirlpoolSwitchDescription(key="ac_display", translation_key="ac_display", icon="mdi:monitor", value_fn=lambda flat: attr_value(flat, "Sys_DisplaySetBrightness") == "4", set_fn=_set_aircon_display_on, aircon_only=True),
    WhirlpoolSwitchDescription(key="ac_horizontal_louver_swing", translation_key="ac_horizontal_louver_swing", icon="mdi:arrow-split-horizontal", value_fn=lambda flat: _to_bool(attr_value(flat, "Cavity_OpSetHorzLouverSwing")), set_fn=_set_aircon_horizontal_louver_swing, aircon_only=True),
    WhirlpoolSwitchDescription(key="time_auto_update", translation_key="time_auto_update", icon="mdi:cloud-download", value_fn=lambda flat: str(attr_value(flat, "DateTimeMode") or attr_value(flat, "XCat_DateTimeMode") or "") == "2", set_fn=_set_time_auto_update, cooking_only=True),
    WhirlpoolSwitchDescription(key="microwave_turntable", translation_key="microwave_turntable", icon="mdi:microwave", value_fn=lambda flat: _to_bool(attr_value(flat, "Mwo_CycleSetTurntable")), set_fn=_set_microwave_turntable, cooking_only=True, microwave_only=True),
)


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        cooking = is_cooking_appliance(appliance)
        aircon = is_aircon_appliance(appliance)
        flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
        has_mwo = microwave_exists(flat)
        for desc in SWITCHES:
            # Do not expose the generic Power switch for ovens / microwave combos,
            # or for unconfirmed non-cooking categories. Read-only support comes
            # first; writable controls are added only after captures/DDM confirm
            # exact command payloads.
            if desc.non_cooking_only:
                continue
            # Do not expose the generic Power switch for ovens / microwave combos.
            # Whirlpool legacy cooking products cannot be safely "powered on" without
            # an explicit mode and target temperature, so the generic switch caused
            # a Home Assistant error when users tapped it. Use the climate entity or
            # set_oven_cook service to start cooking and the Stop Oven/Microwave
            # buttons to cancel.
            if desc.cooking_only and not cooking:
                continue
            if desc.aircon_only and not aircon:
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

    @property
    def icon(self) -> str | None:
        if self.entity_description.key == "quiet_mode":
            return "mdi:volume-off" if self.is_on else "mdi:volume-high"
        return self.entity_description.icon

    async def async_turn_on(self, **kwargs) -> None:
        self._check_service_request(await self.entity_description.set_fn(self.client, self.said, True))
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        self._check_service_request(await self.entity_description.set_fn(self.client, self.said, False))
        await self.coordinator.async_request_refresh()
