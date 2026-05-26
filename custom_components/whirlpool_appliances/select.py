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
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key, is_cooking_appliance, is_refrigerator_appliance, oven_cavity_exists

REFRIGERATOR_TEMP_MAP = {-4: "12", -2: "11", 0: "10", 3: "9", 5: "8"}
REFRIGERATOR_TEMP_MAP_REVERSED = {value: str(key) for key, value in REFRIGERATOR_TEMP_MAP.items()}

OVEN_MODE_CODE_TO_NAME = {
    "0": "Standby",
    "2": "Bake",
    "6": "Convection Bake",
    "8": "Broil",
    "9": "Convection Broil",
    "16": "Convection Roast",
    "24": "Keep Warm",
    "41": "Air Fry",
}
OVEN_MODE_NAME_TO_CODE = {name: code for code, name in OVEN_MODE_CODE_TO_NAME.items()}
OVEN_MODE_NAME_TO_SERVICE = {
    "Standby": "standby",
    "Bake": "bake",
    "Convection Bake": "convect_bake",
    "Broil": "broil",
    "Convection Broil": "convect_broil",
    "Convection Roast": "convect_roast",
    "Keep Warm": "keep_warm",
    "Air Fry": "air_fry",
}
OVEN_MODE_OPTIONS = list(OVEN_MODE_CODE_TO_NAME.values())
LIMITED_OVEN_MODE_OPTIONS_BY_MODEL = {
    "WOC54EC0HS00": ["Bake", "Broil", "Keep Warm"],
}
LIMITED_OVEN_MODE_OPTIONS_BY_DDM = {
    "DDM_COOKING_MINERVA_COMBO_BIO5_V1": ["Bake", "Broil", "Keep Warm"],
}


def _appliance_field(appliance: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = appliance.get(key)
        if value not in (None, "", "0", 0):
            return str(value)
    return None


def _supported_oven_mode_options(appliance: Mapping[str, Any]) -> list[str]:
    model = _appliance_field(appliance, "MODEL_NO", "ModelNumber", "model", "modelNumber")
    if model and model in LIMITED_OVEN_MODE_OPTIONS_BY_MODEL:
        return LIMITED_OVEN_MODE_OPTIONS_BY_MODEL[model]
    ddm = _appliance_field(appliance, "DATA_MODEL_KEY", "dataModelKey", "ddmKey")
    if ddm and ddm in LIMITED_OVEN_MODE_OPTIONS_BY_DDM:
        return LIMITED_OVEN_MODE_OPTIONS_BY_DDM[ddm]
    return OVEN_MODE_OPTIONS


def _cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def _target_temp_celsius(flat: Mapping[str, Any], cavity: str | None) -> float:
    raw = attr_value(flat, f"{_cavity_prefix(cavity)}_CycleSetTargetTemp")
    try:
        value = int(raw) / 10
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    # Whirlpool legacy ovens expect Celsius. 149°C is the closest whole-Celsius
    # equivalent to the common 300°F default.
    return 149.0


def _oven_is_active(flat: Mapping[str, Any], cavity: str | None) -> bool:
    return str(attr_value(flat, f"{_cavity_prefix(cavity)}_OpStatusState") or "") in {"1", "2"}




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
        if is_cooking_appliance(appliance):
            # Oven cooking mode is exposed through the oven climate entity preset mode.
            pass
        else:
            entities.append(WhirlpoolCycleSelect(coordinator, appliance))
        if is_refrigerator_appliance(appliance):
            entities.append(WhirlpoolRefrigeratorTemperatureSelect(coordinator, appliance))
    async_add_entities(entities)


class WhirlpoolOvenModeSelect(WhirlpoolApkEntity, SelectEntity):
    """Oven cooking mode selector for Whirlpool legacy Minerva ovens."""

    _attr_translation_key = "oven_cook_mode"
    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        self._attr_options = _supported_oven_mode_options(appliance)
        suffix = f"{cavity}_cook_mode_select" if cavity else "cook_mode_select"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def current_option(self) -> str | None:
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_CycleSetCommonMode")
        if raw is None:
            return None
        return OVEN_MODE_CODE_TO_NAME.get(str(raw), f"Mode {raw}")

    async def async_select_option(self, option: str) -> None:
        if option not in OVEN_MODE_NAME_TO_CODE:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_value_set",
            )

        if option == "Standby":
            self._check_service_request(await self.client.stop_oven_cavity(self.said, self.cavity))
            await self.coordinator.async_request_refresh()
            return

        code = OVEN_MODE_NAME_TO_CODE[option]

        if _oven_is_active(self.flat_status, self.cavity):
            # When the oven is running, Whirlpool expects a full cook command
            # rather than a bare mode write.
            self._check_service_request(
                await self.client.set_oven_cook(
                    self.said,
                    _target_temp_celsius(self.flat_status, self.cavity),
                    OVEN_MODE_NAME_TO_SERVICE[option],
                    self.cavity,
                )
            )
        else:
            # When the oven is off, store the selected mode without starting it.
            # The target-temperature number entity will preserve this selected
            # mode and start using it when the user enters a temperature.
            self._check_service_request(
                await self.client.send_attributes(
                    self.said,
                    {f"{_cavity_prefix(self.cavity)}_CycleSetCommonMode": code},
                )
            )

        await self.coordinator.async_request_refresh()


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
