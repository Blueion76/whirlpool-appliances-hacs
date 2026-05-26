"""Climate entities for Whirlpool legacy oven cavities."""
from __future__ import annotations

import asyncio

from collections.abc import Mapping
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_ddm_key, appliance_said
from .capabilities import cooking_cavity_capability
from .const import DOMAIN
from .entity import (
    WhirlpoolApkEntity,
    attr_value,
    celsius_to_unit,
    entity_name_from_key,
    is_cooking_appliance,
    oven_cavity_exists,
    unit_to_celsius,
)


OVEN_TEMPS_F = tuple(range(175, 551, 5))
OVEN_TEMPS_C = tuple(round((temp_f - 32) * 5 / 9, 1) for temp_f in OVEN_TEMPS_F)

OVEN_MODE_CODE_TO_PRESET = {
    "0": "Standby",
    "2": "Bake",
    "6": "Convection Bake",
    "8": "Broil",
    "9": "Convection Broil",
    "16": "Convection Roast",
    "24": "Keep Warm",
    "41": "Air Fry",
}
OVEN_PRESET_TO_SERVICE_MODE = {
    "Standby": "standby",
    "Bake": "bake",
    "Convection Bake": "convect_bake",
    "Broil": "broil",
    "Convection Broil": "convect_broil",
    "Convection Roast": "convect_roast",
    "Keep Warm": "keep_warm",
    "Air Fry": "air_fry",
}
OVEN_PRESET_TO_CODE = {name: code for code, name in OVEN_MODE_CODE_TO_PRESET.items()}
OVEN_PRESETS = list(OVEN_MODE_CODE_TO_PRESET.values())

# Fallback only. Prefer DDM/personality capabilities from /api/v2/DeviceDataModel.
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


def _parsed_ddm_for_appliance(coordinator, appliance: Mapping[str, Any]) -> Mapping[str, Any] | None:
    data = coordinator.data or {}
    ddm_key = appliance_ddm_key(appliance) or _appliance_field(appliance, "DATA_MODEL_KEY", "dataModelKey", "ddmKey")
    capabilities = data.get("ddm_capabilities") or {}
    if ddm_key and isinstance(capabilities.get(ddm_key), Mapping):
        parsed = capabilities[ddm_key].get("parsed")
        if isinstance(parsed, Mapping):
            return parsed

    # Fallback by SAID metadata, useful if a future cache key includes the SAID.
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


def _oven_capability(coordinator, appliance: Mapping[str, Any], cavity: str | None) -> Mapping[str, Any] | None:
    return cooking_cavity_capability(_parsed_ddm_for_appliance(coordinator, appliance), cavity)


def _supported_oven_presets(appliance: Mapping[str, Any], capability: Mapping[str, Any] | None = None) -> list[str]:
    if isinstance(capability, Mapping):
        presets = [str(item) for item in capability.get("supported_presets") or [] if item]
        presets = [preset for preset in presets if preset in OVEN_PRESET_TO_SERVICE_MODE and preset != "Standby"]
        if presets:
            return presets

    model = _appliance_field(appliance, "MODEL_NO", "ModelNumber", "model", "modelNumber")
    if model and model in LIMITED_OVEN_MODE_OPTIONS_BY_MODEL:
        return LIMITED_OVEN_MODE_OPTIONS_BY_MODEL[model]

    ddm = _appliance_field(appliance, "DATA_MODEL_KEY", "dataModelKey", "ddmKey")
    if ddm and ddm in LIMITED_OVEN_MODE_OPTIONS_BY_DDM:
        return LIMITED_OVEN_MODE_OPTIONS_BY_DDM[ddm]

    return OVEN_PRESETS


def _cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def _temp_from_tenths(value: Any) -> float | None:
    if value in (None, "", "0", 0):
        return None
    try:
        return int(value) / 10
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _allowed_temperatures(unit: UnitOfTemperature) -> tuple[float, ...]:
    if unit == UnitOfTemperature.FAHRENHEIT:
        return tuple(float(v) for v in OVEN_TEMPS_F)
    return OVEN_TEMPS_C


def _snap_temperature(value: float, unit: UnitOfTemperature) -> float:
    allowed = _allowed_temperatures(unit)
    return min(allowed, key=lambda allowed_value: abs(allowed_value - float(value)))


def _command_celsius(value: float, unit: UnitOfTemperature) -> float:
    """Convert HA display temperature to Whirlpool Celsius command units.

    DDM TempRange values are tenths of °C. Return one decimal place so commands
    can preserve captured defaults like 176.6 °C bake and 287.7 °C broil.
    """
    snapped = _snap_temperature(value, unit)
    celsius = unit_to_celsius(snapped, unit)
    if celsius is None:
        celsius = 176.6
    return round(float(celsius), 1)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WhirlpoolApkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[ClimateEntity] = []

    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance) or not is_cooking_appliance(appliance):
            continue

        flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
        if oven_cavity_exists(flat, "upper"):
            entities.append(WhirlpoolOvenClimate(coordinator, appliance, "upper"))
        if oven_cavity_exists(flat, "lower"):
            entities.append(WhirlpoolOvenClimate(coordinator, appliance, "lower"))

    async_add_entities(entities)


class WhirlpoolOvenClimate(WhirlpoolApkEntity, ClimateEntity):
    """Oven target temperature and cook-mode climate entity."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str | None) -> None:
        self.cavity = cavity
        self._oven_capability = _oven_capability(coordinator, appliance, cavity)
        self._supported_presets = _supported_oven_presets(appliance, self._oven_capability)
        suffix = f"{cavity}_climate" if cavity else "climate"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)
        self._last_preset_mode: str | None = None

    @property
    def preset_modes(self) -> list[str]:
        return self._supported_presets

    @property
    def temperature_unit(self) -> UnitOfTemperature:
        return super().temperature_unit

    def _mode_capability(self, preset: str | None) -> Mapping[str, Any] | None:
        if not preset or not isinstance(self._oven_capability, Mapping):
            return None
        by_preset = self._oven_capability.get("mode_by_preset")
        value = by_preset.get(preset) if isinstance(by_preset, Mapping) else None
        return value if isinstance(value, Mapping) else None

    def _temperature_range(self, preset: str | None = None) -> Mapping[str, Any] | None:
        cap = self._mode_capability(preset or self.preset_mode or self._supported_presets[0])
        if not isinstance(cap, Mapping):
            return None
        temp = cap.get("target_temperature")
        return temp if isinstance(temp, Mapping) else None

    def _all_temperature_ranges(self) -> list[Mapping[str, Any]]:
        if not isinstance(self._oven_capability, Mapping):
            return []
        ranges: list[Mapping[str, Any]] = []
        for mode in self._oven_capability.get("modes") or []:
            if isinstance(mode, Mapping) and isinstance(mode.get("target_temperature"), Mapping):
                ranges.append(mode["target_temperature"])
        return ranges

    def _display_from_celsius(self, value: float | None) -> float | None:
        if value is None:
            return None
        display = celsius_to_unit(value, self.temperature_unit)
        if display is None:
            return None
        return float(round(display)) if self.temperature_unit == UnitOfTemperature.FAHRENHEIT else round(float(display), 1)

    def _range_display_value(self, rng: Mapping[str, Any], key: str) -> float | None:
        raw = rng.get(key)
        if raw is None:
            return None
        return self._display_from_celsius(float(raw))

    def _snap_to_capability_temperature(self, value: float, preset: str | None = None) -> float:
        rng = self._temperature_range(preset)
        if not isinstance(rng, Mapping):
            return _snap_temperature(value, self.temperature_unit)

        min_value = self._range_display_value(rng, "min_c")
        max_value = self._range_display_value(rng, "max_c")
        if min_value is None or max_value is None:
            return _snap_temperature(value, self.temperature_unit)

        step = rng.get("step_f") if self.temperature_unit == UnitOfTemperature.FAHRENHEIT else rng.get("step_c")
        try:
            step_value = float(step or 1)
        except (TypeError, ValueError):
            step_value = 1.0
        if step_value <= 0:
            step_value = 1.0

        clamped = max(float(min_value), min(float(max_value), float(value)))
        snapped = min_value + round((clamped - min_value) / step_value) * step_value
        snapped = max(float(min_value), min(float(max_value), snapped))
        return float(round(snapped)) if self.temperature_unit == UnitOfTemperature.FAHRENHEIT else round(float(snapped), 1)

    @property
    def min_temp(self) -> float:
        ranges = self._all_temperature_ranges()
        values = [self._range_display_value(rng, "min_c") for rng in ranges]
        values = [value for value in values if value is not None]
        return min(values) if values else _allowed_temperatures(self.temperature_unit)[0]

    @property
    def max_temp(self) -> float:
        ranges = self._all_temperature_ranges()
        values = [self._range_display_value(rng, "max_c") for rng in ranges]
        values = [value for value in values if value is not None]
        return max(values) if values else _allowed_temperatures(self.temperature_unit)[-1]

    @property
    def target_temperature_step(self) -> float:
        rng = self._temperature_range()
        if isinstance(rng, Mapping):
            step = rng.get("step_f") if self.temperature_unit == UnitOfTemperature.FAHRENHEIT else rng.get("step_c")
            try:
                if step and float(step) > 0:
                    return float(step)
            except (TypeError, ValueError):
                pass
        return 5

    @property
    def hvac_mode(self) -> HVACMode:
        state = str(attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_OpStatusState") or "")
        return HVACMode.HEAT if state in {"1", "2"} else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        state = str(attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_OpStatusState") or "")
        if state == "1":
            return HVACAction.PREHEATING
        if state == "2":
            return HVACAction.HEATING
        if state in {"0", "4"}:
            return HVACAction.OFF
        return None

    @property
    def current_temperature(self) -> float | None:
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_DisplStatusDisplayTemp")
        value = _temp_from_tenths(raw)
        return celsius_to_unit(value, self.temperature_unit) if value is not None else None

    @property
    def target_temperature(self) -> float | None:
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_CycleSetTargetTemp")
        value = _temp_from_tenths(raw)
        if value is None:
            return None

        display = celsius_to_unit(value, self.temperature_unit)
        if display is None:
            return None
        return self._snap_to_capability_temperature(display)

    def _active_preset_fallback(self) -> str | None:
        """Fallback when Minerva reports active state but leaves mode at 0.

        Some WOC/Minerva ovens keep CycleSetCommonMode at 0 after a remote
        command, even while the appliance is actually baking/broiling. Use only
        the last preset HA commanded during this runtime. Do not infer from
        target temperature because a high-temp bake can otherwise appear as
        broil.
        """
        if self._last_preset_mode and self._last_preset_mode != "Standby":
            return self._last_preset_mode
        return None

    @property
    def preset_mode(self) -> str | None:
        raw = attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_CycleSetCommonMode")
        state = str(attr_value(self.flat_status, f"{_cavity_prefix(self.cavity)}_OpStatusState") or "")
        if raw in (None, "0", 0) and state in {"1", "2"}:
            # The appliance can keep reporting mode 0 even after it accepts a
            # Broil/Keep Warm/Bake command. Use only the last HA-commanded
            # preset; after a restart, show unknown instead of guessing.
            return self._active_preset_fallback()
        if raw is None:
            return None
        return OVEN_MODE_CODE_TO_PRESET.get(str(raw), f"Mode {raw}")

    def _selected_service_mode(self) -> str:
        preset = self.preset_mode
        if preset and preset in OVEN_PRESET_TO_SERVICE_MODE and preset != "Standby":
            return OVEN_PRESET_TO_SERVICE_MODE[preset]
        return "bake"

    def _default_target_temperature_for_preset(self, preset: str | None) -> float:
        rng = self._temperature_range(preset)
        if isinstance(rng, Mapping):
            value = self._range_display_value(rng, "default_c")
            if value is not None:
                return value

        # Fallback app-observed defaults.
        if preset in {"Broil", "Convection Broil"}:
            temp_f = 550
        elif preset == "Keep Warm":
            temp_f = 170
        else:
            temp_f = 350
        return temp_f if self.temperature_unit == UnitOfTemperature.FAHRENHEIT else round((temp_f - 32) * 5 / 9, 1)

    def _default_target_temperature(self) -> float:
        return self._default_target_temperature_for_preset(self.preset_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        # Whirlpool/Minerva ovens may reject an in-place target-temperature
        # change while already preheating/cooking. Cancel the current operation
        # first, then send a fresh full command containing the selected mode and
        # rounded whole-Celsius target temp.
        if self.hvac_mode == HVACMode.HEAT:
            self._check_service_request(await self.client.stop_oven_cavity(self.said, self.cavity))
            # Give the appliance/cloud state a moment to accept the stop before
            # the new start command. This avoids back-to-back command NACKs on
            # WOC/Minerva combo ovens.
            await asyncio.sleep(1)

        self._check_service_request(
            await self.client.set_oven_cook(
                self.said,
                unit_to_celsius(self._snap_to_capability_temperature(float(temperature)), self.temperature_unit) or 176.6,
                self._selected_service_mode(),
                self.cavity,
            )
        )
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in OVEN_PRESET_TO_SERVICE_MODE or preset_mode not in self._supported_presets + ["Standby"]:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_value_set",
            )

        if preset_mode == "Standby":
            await self.async_turn_off()
            return

        # Minerva ovens often reject in-place mode changes once preheating or
        # cooking. Treat a preset/mode change like a temperature change: stop the
        # current oven operation first, then immediately send a fresh complete
        # command with the new mode and target temperature.
        was_heating = self.hvac_mode == HVACMode.HEAT
        if was_heating:
            self._check_service_request(await self.client.stop_oven_cavity(self.said, self.cavity))
            await asyncio.sleep(1)

        target = self.target_temperature or self._default_target_temperature_for_preset(preset_mode)
        self._last_preset_mode = preset_mode
        self._check_service_request(
            await self.client.set_oven_cook(
                self.said,
                unit_to_celsius(self._snap_to_capability_temperature(float(target), preset_mode), self.temperature_unit) or 176.6,
                OVEN_PRESET_TO_SERVICE_MODE[preset_mode],
                self.cavity,
            )
        )
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
            return
        if hvac_mode == HVACMode.HEAT:
            # Turning the climate entity to Heat in the UI should start the oven
            # using the selected/default preset and target temperature.
            await self.async_turn_on()
            return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="invalid_value_set",
        )

    async def async_turn_on(self) -> None:
        target = self.target_temperature or self._default_target_temperature()
        current_preset = self.preset_mode
        if current_preset and current_preset != "Standby":
            self._last_preset_mode = current_preset
        self._check_service_request(
            await self.client.set_oven_cook(
                self.said,
                unit_to_celsius(self._snap_to_capability_temperature(float(target), current_preset), self.temperature_unit) or 176.6,
                self._selected_service_mode(),
                self.cavity,
            )
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        self._last_preset_mode = None
        self._check_service_request(await self.client.stop_oven_cavity(self.said, self.cavity))
        await self.coordinator.async_request_refresh()
