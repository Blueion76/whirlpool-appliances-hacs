"""Select entities for cycle/control values discovered in appliance status."""
from __future__ import annotations

import asyncio

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
from .oven_options import FROZEN_BAKE_FOOD_OPTIONS, current_oven_options, local_options, minutes_to_seconds, oven_is_active, update_local_options

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
OVEN_COMPLETE_ACTION_CODE_TO_NAME = {
    "1": "Stay On",
    "2": "Keep Warm",
    "3": "Turn Off",
}
OVEN_COMPLETE_ACTION_NAME_TO_SERVICE = {
    "Stay On": "stay_on",
    "Keep Warm": "keep_warm",
    "Turn Off": "turn_off",
}
OVEN_COMPLETE_ACTION_CODE_TO_SERVICE = {code: OVEN_COMPLETE_ACTION_NAME_TO_SERVICE[name] for code, name in OVEN_COMPLETE_ACTION_CODE_TO_NAME.items()}
OVEN_COMPLETE_ACTION_NAME_TO_CODE = {name: code for code, name in OVEN_COMPLETE_ACTION_CODE_TO_NAME.items()}
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


def _seconds_value(flat: Mapping[str, Any], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _current_oven_options(flat: Mapping[str, Any], cavity: str | None) -> dict[str, Any]:
    prefix = _cavity_prefix(cavity)
    mode_code = str(attr_value(flat, f"{prefix}_CycleSetCommonMode") or "")
    mode = OVEN_MODE_NAME_TO_SERVICE.get(OVEN_MODE_CODE_TO_NAME.get(mode_code, "Bake"), "bake")
    action_code = str(attr_value(flat, f"{prefix}_OpSetCookTimeCompleteAction") or "3")
    return {
        "mode": mode,
        "target_temp": _target_temp_celsius(flat, cavity),
        "cook_time_seconds": _seconds_value(flat, f"{prefix}_TimeSetCookTimeSet"),
        "delay_time_seconds": _seconds_value(flat, f"{prefix}_TimeSetDelayTime"),
        "complete_action": OVEN_COMPLETE_ACTION_CODE_TO_SERVICE.get(action_code, "turn_off"),
    }




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
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            if oven_cavity_exists(flat, "upper"):
                entities.append(WhirlpoolOvenModeSelect(coordinator, appliance, "upper"))
                entities.append(WhirlpoolOvenCompleteActionSelect(coordinator, appliance, "upper"))
                entities.append(WhirlpoolFrozenBakePresetSelect(coordinator, appliance, "upper"))
            if oven_cavity_exists(flat, "lower"):
                entities.append(WhirlpoolOvenModeSelect(coordinator, appliance, "lower"))
                entities.append(WhirlpoolOvenCompleteActionSelect(coordinator, appliance, "lower"))
                entities.append(WhirlpoolFrozenBakePresetSelect(coordinator, appliance, "lower"))
        else:
            entities.append(WhirlpoolCycleSelect(coordinator, appliance))
        if is_refrigerator_appliance(appliance):
            entities.append(WhirlpoolRefrigeratorTemperatureSelect(coordinator, appliance))
    async_add_entities(entities)


class WhirlpoolOvenCompleteActionSelect(WhirlpoolApkEntity, SelectEntity):
    """Cook-time complete action selector."""

    _attr_translation_key = "oven_complete_action"

    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        self._attr_options = list(OVEN_COMPLETE_ACTION_CODE_TO_NAME.values())
        suffix = f"{cavity}_oven_complete_action" if cavity else "oven_complete_action"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def current_option(self) -> str | None:
        local = local_options(self.coordinator, self.said, self.cavity)
        if "complete_action" in local:
            service = local["complete_action"]
            for name, mapped in OVEN_COMPLETE_ACTION_NAME_TO_SERVICE.items():
                if mapped == service:
                    return name
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_OpSetCookTimeCompleteAction")
        if raw in (None, "", "0", 0):
            return "Turn Off"
        return OVEN_COMPLETE_ACTION_CODE_TO_NAME.get(str(raw), f"Action {raw}")

    async def async_select_option(self, option: str) -> None:
        code = OVEN_COMPLETE_ACTION_NAME_TO_CODE.get(option)
        complete_action = OVEN_COMPLETE_ACTION_NAME_TO_SERVICE.get(option)
        if code is None or complete_action is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")

        # Complete action is part of the full oven cycle payload. While cooking,
        # cancel and restart with the existing options. While idle, store it
        # locally for the Start Oven button instead of sending a partial write.
        if oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["complete_action"] = complete_action
            self._check_service_request(await self.client.stop_oven_cavity(self.said, self.cavity))
            await asyncio.sleep(1)
            self._check_service_request(
                await self.client.set_oven_cook(
                    self.said,
                    float(options["target_temp"]),
                    str(options["mode"]),
                    self.cavity,
                    cook_time_seconds=minutes_to_seconds(options.get("cook_time_minutes")),
                    delay_time_seconds=minutes_to_seconds(options.get("delay_time_minutes")),
                    complete_action=str(options["complete_action"]),
                )
            )
            await self.coordinator.async_request_refresh()
            return

        update_local_options(self.coordinator, self.said, self.cavity, complete_action=complete_action)
        self.async_write_ha_state()



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
        local = local_options(self.coordinator, self.said, self.cavity)
        if "mode" in local:
            service = local["mode"]
            for name, mapped in OVEN_MODE_NAME_TO_SERVICE.items():
                if mapped == service:
                    return name
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_CycleSetCommonMode")
        if raw is None:
            return "Bake"
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

        mode = OVEN_MODE_NAME_TO_SERVICE[option]

        if oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["mode"] = mode
            options["frozen_food"] = None
            self._check_service_request(await self.client.stop_oven_cavity(self.said, self.cavity))
            await asyncio.sleep(1)
            self._check_service_request(
                await self.client.set_oven_cook(
                    self.said,
                    float(options["target_temp"]),
                    str(options["mode"]),
                    self.cavity,
                    cook_time_seconds=minutes_to_seconds(options.get("cook_time_minutes")),
                    delay_time_seconds=minutes_to_seconds(options.get("delay_time_minutes")),
                    complete_action=str(options["complete_action"]),
                )
            )
            await self.coordinator.async_request_refresh()
            return

        update_local_options(self.coordinator, self.said, self.cavity, mode=mode, frozen_food=None)
        self.async_write_ha_state()


class WhirlpoolFrozenBakePresetSelect(WhirlpoolApkEntity, SelectEntity):
    """Local Frozen Bake preset selector used by the Start Oven button."""

    _attr_translation_key = "frozen_bake_preset"

    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        self._attr_options = FROZEN_BAKE_FOOD_OPTIONS
        suffix = f"{cavity}_frozen_bake_preset" if cavity else "frozen_bake_preset"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def current_option(self) -> str | None:
        food = local_options(self.coordinator, self.said, self.cavity).get("frozen_food")
        return str(food).replace("_", " ").title() if food else "None"

    async def async_select_option(self, option: str) -> None:
        if option not in FROZEN_BAKE_FOOD_OPTIONS:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        update_local_options(
            self.coordinator,
            self.said,
            self.cavity,
            frozen_food=None if option == "None" else option.lower().replace(" ", "_"),
        )
        self.async_write_ha_state()



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
