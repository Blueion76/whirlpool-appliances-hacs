"""Select entities for cycle/control values discovered in appliance status and DDM capabilities."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_ddm_key, appliance_said
from .helpers.capabilities import cooking_cavity_capability
from .const import DOMAIN
from .helpers.control import (
    CLEAN_MODE_CODE_TO_NAME,
    CLEAN_MODE_NAME_TO_CODE,
    DISPLAY_LANGUAGE_CODE_TO_NAME,
    DISPLAY_LANGUAGE_NAME_TO_CODE,
    MWO_DONENESS_CODE_TO_NAME,
    MWO_DONENESS_NAME_TO_CODE,
    MWO_MODE_OPTIONS,
    MWO_PRESETS_BY_MODE,
    TEMPERATURE_UNITS_CODE_TO_NAME,
    TEMPERATURE_UNITS_NAME_TO_CODE,
    TONE_VOLUME_CODE_TO_NAME,
    TONE_VOLUME_NAME_TO_CODE,
    frozen_or_custom_cycle,
    microwave_local_options,
    oven_cook_attrs,
    oven_is_active,
    raise_if_common_blocked,
    update_microwave_options,
)
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key, is_cooking_appliance, is_refrigerator_appliance, microwave_exists, oven_cavity_exists
from .helpers.logging import summarize
from .helpers.oven_options import current_oven_options, local_options, minutes_to_seconds, update_local_options

OVEN_MODE_CODE_TO_NAME = {"0": "Standby", "2": "Bake", "6": "Convection Bake", "8": "Broil", "9": "Convection Broil", "16": "Convection Roast", "24": "Keep Warm", "41": "Air Fry"}
OVEN_MODE_NAME_TO_SERVICE = {"Standby": "standby", "Bake": "bake", "Convection Bake": "convect_bake", "Broil": "broil", "Convection Broil": "convect_broil", "Convection Roast": "convect_roast", "Keep Warm": "keep_warm", "Air Fry": "air_fry"}
OVEN_COMPLETE_ACTION_CODE_TO_NAME = {"1": "Stay On", "2": "Keep Warm", "3": "Turn Off"}
OVEN_COMPLETE_ACTION_NAME_TO_SERVICE = {"Stay On": "stay_on", "Keep Warm": "keep_warm", "Turn Off": "turn_off"}
REFRIGERATOR_TEMP_MAP = {-4: "12", -2: "11", 0: "10", 3: "9", 5: "8"}
REFRIGERATOR_TEMP_MAP_REVERSED = {value: str(key) for key, value in REFRIGERATOR_TEMP_MAP.items()}


def _appliance_field(appliance: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = appliance.get(key)
        if value not in (None, "", "0", 0):
            return str(value)
    return None


def _parsed_ddm_for_appliance(coordinator, appliance: Mapping[str, Any]) -> Mapping[str, Any] | None:
    data = coordinator.data or {}
    ddm_key = appliance_ddm_key(appliance) or _appliance_field(appliance, "DATA_MODEL_KEY", "dataModelKey", "ddmKey")
    capabilities = data.get("ddm_capabilities") or {}
    if ddm_key and isinstance(capabilities.get(ddm_key), Mapping):
        parsed = capabilities[ddm_key].get("parsed")
        if isinstance(parsed, Mapping):
            return parsed
    said = appliance_said(appliance)
    if said:
        for entry in capabilities.values():
            if not isinstance(entry, Mapping):
                continue
            metadata = entry.get("metadata") or {}
            if said in (metadata.get("appliance_saids") or []):
                parsed = entry.get("parsed")
                if isinstance(parsed, Mapping):
                    return parsed
    return None


def _attr_supported(coordinator, appliance: Mapping[str, Any], attr: str, *, writable: bool = True) -> bool:
    parsed = _parsed_ddm_for_appliance(coordinator, appliance)
    if not isinstance(parsed, Mapping):
        return False
    key = "writable_attributes" if writable else "readable_attributes"
    return attr in set(parsed.get(key) or [])


def _cavity_capability(coordinator, appliance: Mapping[str, Any], cavity: str | None) -> Mapping[str, Any] | None:
    return cooking_cavity_capability(_parsed_ddm_for_appliance(coordinator, appliance), cavity)


def _supported_oven_mode_options(coordinator, appliance: Mapping[str, Any], cavity: str | None) -> list[str]:
    capability = _cavity_capability(coordinator, appliance, cavity)
    if not isinstance(capability, Mapping):
        return []
    presets = capability.get("supported_presets")
    if not isinstance(presets, list):
        return []
    return [str(preset) for preset in presets if str(preset) in OVEN_MODE_NAME_TO_SERVICE and str(preset) != "Standby"]


def _frozen_bake_options(coordinator, appliance: Mapping[str, Any], cavity: str | None) -> list[str]:
    capability = _cavity_capability(coordinator, appliance, cavity)
    frozen = capability.get("frozen_bake") if isinstance(capability, Mapping) else None
    foods = frozen.get("foods") if isinstance(frozen, Mapping) else None
    options = [str(item.get("food")).replace("_", " ").title() for item in foods or [] if isinstance(item, Mapping) and item.get("food")]
    return ["None", *options] if options else []


def _frozen_bake_defaults(coordinator, appliance: Mapping[str, Any], cavity: str | None, food: str) -> dict[str, Any]:
    capability = _cavity_capability(coordinator, appliance, cavity)
    frozen = capability.get("frozen_bake") if isinstance(capability, Mapping) else None
    food_by_name = frozen.get("food_by_name") if isinstance(frozen, Mapping) else None
    details = food_by_name.get(food) if isinstance(food_by_name, Mapping) else None
    if not isinstance(details, Mapping):
        return {}
    updates: dict[str, Any] = {}
    target = details.get("target_temperature")
    if isinstance(target, Mapping) and target.get("default_c") is not None:
        # DDM capability defaults are Celsius, but legacy oven commands use Fahrenheit.
        updates["target_temp"] = round(float(target["default_c"]) * 9 / 5 + 32)
    cook_time = details.get("cook_time")
    if isinstance(cook_time, Mapping) and cook_time.get("default") is not None:
        try:
            minutes = float(cook_time["default"]) / 60
            updates["cook_time_minutes"] = int(minutes) if minutes.is_integer() else round(minutes, 1)
        except (TypeError, ValueError):
            pass
    complete = details.get("cook_time_complete_action")
    if isinstance(complete, Mapping):
        default = str(complete.get("default") or "")
        if default == "1":
            updates["complete_action"] = "stay_on"
        elif default == "2":
            updates["complete_action"] = "keep_warm"
        elif default == "3":
            updates["complete_action"] = "turn_off"
    return updates


def _cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if str(cavity).lower().startswith("lower") else "OvenUpperCavity"


async def _send_oven_options(entity: WhirlpoolApkEntity, cavity: str | None, options: Mapping[str, Any]) -> None:
    active = oven_is_active(entity.flat_status, cavity)
    raise_if_common_blocked(entity.flat_status, cavity=cavity)
    if active and frozen_or_custom_cycle(entity.flat_status, cavity):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="modify_not_allowed")
    attrs = oven_cook_attrs(cavity=cavity, temperature=float(options["target_temp"]), mode=str(options["mode"]), cook_time_seconds=minutes_to_seconds(options.get("cook_time_minutes")), delay_time_seconds=minutes_to_seconds(options.get("delay_time_minutes")), complete_action=str(options["complete_action"]), operation="4" if active else "2")
    entity._check_service_request(await entity.client.send_attributes(entity.said, attrs))
    await entity.coordinator.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities: list[SelectEntity] = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            for cavity in ("upper", "lower"):
                if not oven_cavity_exists(flat, cavity):
                    continue
                if _supported_oven_mode_options(coordinator, appliance, cavity):
                    entities.append(WhirlpoolOvenModeSelect(coordinator, appliance, cavity))
                    entities.append(WhirlpoolOvenCompleteActionSelect(coordinator, appliance, cavity))
                if _frozen_bake_options(coordinator, appliance, cavity):
                    entities.append(WhirlpoolFrozenBakePresetSelect(coordinator, appliance, cavity))
                if _attr_supported(coordinator, appliance, f"{_cavity_prefix(cavity)}_CycleSetCleanOvenMode"):
                    entities.append(WhirlpoolCleanModeSelect(coordinator, appliance, cavity))
            for key, attr, name_to_code, code_to_name, icon in (
                ("display_language", "Sys_DisplaySetLanguage", DISPLAY_LANGUAGE_NAME_TO_CODE, DISPLAY_LANGUAGE_CODE_TO_NAME, "mdi:translate"),
                ("temperature_units", "Sys_DisplaySetTempUnits", TEMPERATURE_UNITS_NAME_TO_CODE, TEMPERATURE_UNITS_CODE_TO_NAME, "mdi:thermometer-lines"),
                ("keypress_tone_volume", "Sys_OperationSetKeyPressToneVolume", TONE_VOLUME_NAME_TO_CODE, TONE_VOLUME_CODE_TO_NAME, "mdi:volume-high"),
                ("alert_tone_volume", "Sys_OperationSetAlertToneVolume", TONE_VOLUME_NAME_TO_CODE, TONE_VOLUME_CODE_TO_NAME, "mdi:bell-ring"),
            ):
                if _attr_supported(coordinator, appliance, attr):
                    entities.append(WhirlpoolSimpleAttributeSelect(coordinator, appliance, key, attr, name_to_code, code_to_name, icon))
            if microwave_exists(flat):
                entities += [WhirlpoolMicrowaveModeSelect(coordinator, appliance), WhirlpoolMicrowavePresetSelect(coordinator, appliance), WhirlpoolMicrowaveDonenessSelect(coordinator, appliance)]
        else:
            entities.append(WhirlpoolCycleSelect(coordinator, appliance))
        if is_refrigerator_appliance(appliance):
            entities.append(WhirlpoolRefrigeratorTemperatureSelect(coordinator, appliance))
    async_add_entities(entities)


class WhirlpoolOvenCompleteActionSelect(WhirlpoolApkEntity, SelectEntity):
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
        complete_action = OVEN_COMPLETE_ACTION_NAME_TO_SERVICE.get(option)
        if complete_action is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        if oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["complete_action"] = complete_action
            await _send_oven_options(self, self.cavity, options)
            return
        update_local_options(self.coordinator, self.said, self.cavity, complete_action=complete_action)
        self.async_write_ha_state()


class WhirlpoolOvenModeSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "oven_cook_mode"
    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        self._attr_options = _supported_oven_mode_options(coordinator, appliance, cavity)
        self._default_option = self._attr_options[0] if self._attr_options else None
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
        if not oven_is_active(self.flat_status, self.cavity):
            return self._default_option
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_CycleSetCommonMode")
        return OVEN_MODE_CODE_TO_NAME.get(str(raw), f"Mode {raw}") if raw is not None else self._default_option

    async def async_select_option(self, option: str) -> None:
        if option not in self.options or option not in OVEN_MODE_NAME_TO_SERVICE:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        mode = OVEN_MODE_NAME_TO_SERVICE[option]
        if oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["mode"] = mode
            options["frozen_food"] = None
            await _send_oven_options(self, self.cavity, options)
            return
        update_local_options(self.coordinator, self.said, self.cavity, mode=mode, frozen_food=None)
        self.async_write_ha_state()


class WhirlpoolFrozenBakePresetSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "frozen_bake_preset"
    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        self._attr_options = _frozen_bake_options(coordinator, appliance, cavity)
        suffix = f"{cavity}_frozen_bake_preset" if cavity else "frozen_bake_preset"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def current_option(self) -> str | None:
        food = local_options(self.coordinator, self.said, self.cavity).get("frozen_food")
        return str(food).replace("_", " ").title() if food else "None"

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        if oven_is_active(self.flat_status, self.cavity):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="modify_not_allowed")
        if option == "None":
            update_local_options(self.coordinator, self.said, self.cavity, frozen_food=None)
        else:
            food = option.lower().replace(" ", "_")
            updates = _frozen_bake_defaults(self.coordinator, self.appliance, self.cavity, food)
            update_local_options(self.coordinator, self.said, self.cavity, frozen_food=food, **updates)
        self.coordinator.async_update_listeners()
        self.async_write_ha_state()


class WhirlpoolCleanModeSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "oven_clean_mode"
    _attr_icon = "mdi:spray-bottle"
    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        self._attr_options = ["None", "Standard", "Mid", "Low", "Steam"]
        suffix = f"{cavity}_oven_clean_mode" if cavity else "oven_clean_mode"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def current_option(self) -> str | None:
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_CycleSetCleanOvenMode")
        return CLEAN_MODE_CODE_TO_NAME.get(str(raw or "0"), "None")

    async def async_select_option(self, option: str) -> None:
        code = CLEAN_MODE_NAME_TO_CODE.get(option)
        if code is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        raise_if_common_blocked(self.flat_status, cavity=self.cavity)
        if oven_is_active(self.flat_status, self.cavity):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="oven_active")
        self._check_service_request(await self.client.send_attributes(self.said, {f"{_cavity_prefix(self.cavity)}_CycleSetCleanOvenMode": code}))
        await self.coordinator.async_request_refresh()


class WhirlpoolSimpleAttributeSelect(WhirlpoolApkEntity, SelectEntity):
    def __init__(self, coordinator, appliance: Mapping[str, Any], key: str, attr: str, name_to_code: Mapping[str, str], code_to_name: Mapping[str, str], icon: str | None = None) -> None:
        self.attr = attr
        self.name_to_code = dict(name_to_code)
        self.code_to_name = dict(code_to_name)
        self._attr_options = list(name_to_code)
        self._attr_icon = icon
        super().__init__(coordinator, appliance, key)
        self._attr_translation_key = key
        self._attr_name = entity_name_from_key(key, appliance)

    @property
    def current_option(self) -> str | None:
        raw = attr_value(self.flat_status, self.attr)
        return self.code_to_name.get(str(raw), self._attr_options[0] if self._attr_options else None)

    async def async_select_option(self, option: str) -> None:
        code = self.name_to_code.get(option)
        if code is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        raise_if_common_blocked(self.flat_status)
        self._check_service_request(await self.client.send_attributes(self.said, {self.attr: code}))
        await self.coordinator.async_request_refresh()


class WhirlpoolMicrowaveModeSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "microwave_mode"
    _attr_icon = "mdi:microwave"
    _attr_options = MWO_MODE_OPTIONS
    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "microwave_mode")
        self._attr_name = entity_name_from_key("microwave_mode", appliance)

    @property
    def current_option(self) -> str | None:
        return str(microwave_local_options(self.coordinator, self.said).get("mode") or "Cook")

    async def async_select_option(self, option: str) -> None:
        if option not in MWO_MODE_OPTIONS:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        first_preset = next(iter(MWO_PRESETS_BY_MODE.get(option, {"Manual": "1"})))
        update_microwave_options(self.coordinator, self.said, mode=option, preset=first_preset)
        self.coordinator.async_update_listeners()
        self.async_write_ha_state()


class WhirlpoolMicrowavePresetSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "microwave_preset"
    _attr_icon = "mdi:format-list-bulleted"
    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "microwave_preset")
        self._attr_name = entity_name_from_key("microwave_preset", appliance)

    @property
    def options(self) -> list[str]:
        mode = str(microwave_local_options(self.coordinator, self.said).get("mode") or "Cook")
        return list(MWO_PRESETS_BY_MODE.get(mode, {"Manual": "1"}))

    @property
    def current_option(self) -> str | None:
        option = str(microwave_local_options(self.coordinator, self.said).get("preset") or "")
        return option if option in self.options else (self.options[0] if self.options else None)

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        update_microwave_options(self.coordinator, self.said, preset=option)
        self.async_write_ha_state()


class WhirlpoolMicrowaveDonenessSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "microwave_doneness"
    _attr_icon = "mdi:tune"
    _attr_options = list(MWO_DONENESS_NAME_TO_CODE)
    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "microwave_doneness")
        self._attr_name = entity_name_from_key("microwave_doneness", appliance)

    @property
    def current_option(self) -> str | None:
        local = microwave_local_options(self.coordinator, self.said).get("doneness")
        if local:
            return str(local)
        return MWO_DONENESS_CODE_TO_NAME.get(str(attr_value(self.flat_status, "Mwo_CycleSetDoneness") or "0"), "Default")

    async def async_select_option(self, option: str) -> None:
        if option not in MWO_DONENESS_NAME_TO_CODE:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
        update_microwave_options(self.coordinator, self.said, doneness=option)
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
            return [str(item.get("name") or item.get("cycleName") or item.get("id") if isinstance(item, Mapping) else item) for item in value if item is not None]
        return []

    @property
    def current_option(self) -> str | None:
        value = find_key(self.flat_status, ("cycle", "cycleName", "currentCycle"))
        return str(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        self._check_service_request(await self.client.send_appliance_command(self.said, "setCycle", {"cycle": option}))
        await self.coordinator.async_request_refresh()


class WhirlpoolRefrigeratorTemperatureSelect(WhirlpoolApkEntity, SelectEntity):
    _attr_translation_key = "refrigerator_temperature_level"
    _attr_options = [str(option) for option in REFRIGERATOR_TEMP_MAP]
    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "refrigerator_temperature_level")
        self._attr_name = entity_name_from_key("refrigerator_temperature_level")

    @property
    def current_option(self) -> str | None:
        raw = attr_value(self.flat_status, "Refrigerator_OpSetTempPreset")
        return REFRIGERATOR_TEMP_MAP_REVERSED.get(str(raw), str(raw)) if raw is not None else None

    async def async_select_option(self, option: str) -> None:
        try:
            mapped = REFRIGERATOR_TEMP_MAP[int(option)]
        except (KeyError, ValueError) as err:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set") from err
        self._check_service_request(await self.client.send_attributes(self.said, {"Refrigerator_OpSetTempPreset": mapped}))
        await self.coordinator.async_request_refresh()
