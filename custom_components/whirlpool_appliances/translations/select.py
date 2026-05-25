"""Select entities for cycle/control values discovered in appliance status."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .const import DOMAIN
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key, is_cooking_appliance, is_refrigerator_appliance

REFRIGERATOR_TEMP_MAP = {-4: "12", -2: "11", 0: "10", 3: "9", 5: "8"}
REFRIGERATOR_TEMP_MAP_REVERSED = {value: str(key) for key, value in REFRIGERATOR_TEMP_MAP.items()}


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        # The generic cycle selector was useful for laundry payloads that expose
        # availableCycles/supportedCycles. Combo cooking appliances like
        # DDM_COOKING_MINERVA_COMBO_BIO5_V1 expose oven/microwave mode attributes
        # instead, so a generic cycle selector is misleading and cannot work.
        if not is_cooking_appliance(appliance):
            entities.append(WhirlpoolCycleSelect(coordinator, appliance))
        if is_refrigerator_appliance(appliance):
            entities.append(WhirlpoolRefrigeratorTemperatureSelect(coordinator, appliance))
    async_add_entities(entities)


class WhirlpoolCycleSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "cycle_select"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "cycle_select")
        self._attr_name = entity_name_from_key("cycle_select", appliance)

    @property
    def available(self) -> bool:
        return super().available and bool(self.options)

    @property
    def options(self) -> list[str]:
        value = find_key(self.flat_status, ("availableCycles", "cycles", "supportedCycles"))
        if isinstance(value, list):
            opts: list[str] = []
            for item in value:
                if isinstance(item, Mapping):
                    name = item.get("name") or item.get("cycleName") or item.get("id")
                    if name:
                        opts.append(str(name))
                elif item is not None:
                    opts.append(str(item))
            return opts
        return []

    @property
    def current_option(self) -> str | None:
        value = find_key(self.flat_status, ("cycle", "cycleName", "currentCycle"))
        return str(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        self._check_service_request(await self.client.send_appliance_command(self.said, "setCycle", {"cycle": option}))
        await self.coordinator.async_request_refresh()


class WhirlpoolRefrigeratorTemperatureSelect(WhirlpoolApkEntity, SelectEntity):
    """Official Whirlpool refrigerator temperature-level select."""

    _attr_translation_key = "refrigerator_temperature_level"
    _attr_options = [str(option) for option in REFRIGERATOR_TEMP_MAP]
    _attr_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "refrigerator_temperature_level")
        self._attr_name = entity_name_from_key("refrigerator_temperature_level")

    @property
    def current_option(self) -> str | None:
        raw = attr_value(self.flat_status, "Refrigerator_OpSetTempPreset")
        if raw is None:
            return None
        return REFRIGERATOR_TEMP_MAP_REVERSED.get(str(raw), str(raw))

    async def async_select_option(self, option: str) -> None:
        try:
            mapped = REFRIGERATOR_TEMP_MAP[int(option)]
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_value_set",
            ) from err
        self._check_service_request(await self.client.send_attributes(self.said, {"Refrigerator_OpSetTempPreset": mapped}))
        await self.coordinator.async_request_refresh()
