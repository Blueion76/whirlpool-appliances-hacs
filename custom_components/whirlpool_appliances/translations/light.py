"""Light entities for Whirlpool oven cavity lights when present."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, is_cooking_appliance, microwave_exists, oven_cavity_exists


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower() in ("1", "true", "on", "yes")
    return bool(value)


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            if oven_cavity_exists(flat, "upper"):
                entities.append(WhirlpoolCavityLight(coordinator, appliance, "upper"))
            if oven_cavity_exists(flat, "lower"):
                entities.append(WhirlpoolCavityLight(coordinator, appliance, "lower"))
            if microwave_exists(flat):
                entities.append(WhirlpoolCavityLight(coordinator, appliance, "microwave"))
        else:
            entities.append(WhirlpoolCavityLight(coordinator, appliance, None))
    async_add_entities(entities)


class WhirlpoolCavityLight(WhirlpoolApkEntity, LightEntity):
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_color_mode = ColorMode.ONOFF

    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        suffix = f"{cavity}_light" if cavity == "microwave" else (f"{cavity}_cavity_light" if cavity else "cavity_light")
        super().__init__(coordinator, appliance, suffix)
        self._attr_translation_key = suffix
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def is_on(self) -> bool | None:
        if self.cavity == "microwave":
            return _to_bool(attr_value(self.flat_status, "Mwo_DisplaySetLightOn"))
        if self.cavity == "lower":
            return _to_bool(attr_value(self.flat_status, "OvenLowerCavity_DisplaySetLightOn"))
        if self.cavity == "upper":
            return _to_bool(attr_value(self.flat_status, "OvenUpperCavity_DisplaySetLightOn"))
        return _to_bool(attr_value(self.flat_status, "OvenUpperCavity_DisplaySetLightOn"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._check_service_request(await self.client.set_cavity_light(self.said, True, self.cavity))
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._check_service_request(await self.client.set_cavity_light(self.said, False, self.cavity))
        await self.coordinator.async_request_refresh()
