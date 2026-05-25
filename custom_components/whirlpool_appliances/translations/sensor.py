"""Sensors for Whirlpool Appliances integration."""
from __future__ import annotations

import json
from datetime import timedelta
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.util import dt as dt_util
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .const import CONF_EXPOSE_RAW_SENSORS
from .entity import WhirlpoolApkEntity, attr_value, celsius_to_unit, entity_name_from_key, find_key, is_cooking_appliance, microwave_exists, oven_cavity_exists


CAVITY_STATE = {"0": "Standby", "1": "Preheating", "2": "Cooking", "4": "Not Present"}
COOK_MODE = {
    "0": "Standby",
    "2": "Bake",
    "6": "Convection Bake",
    "8": "Broil",
    "9": "Convection Broil",
    "16": "Convection Roast",
    "24": "Keep Warm",
    "41": "Air Fry",
}
TIMER_STATE = {"0": "Standby", "1": "Running", "3": "Completed"}
MACHINE_STATE = {"0": "Standby", "1": "Setting", "2": "Delay Countdown", "3": "Delay Paused", "4": "Smart Delay", "5": "Smart Grid Pause", "6": "Pause", "7": "Running Main Cycle", "8": "Running Post Cycle", "9": "Exception", "10": "Complete", "11": "Power Failure", "12": "Service Diagnostic Mode", "13": "Factory Diagnostic Mode", "14": "Life Test", "15": "Customer Focus Mode", "16": "Demo Mode", "17": "Hard Stop Or Error", "18": "System Initialize", "19": "Cancelled"}
# Observed on WOC54EC0HS00 combo models. State 4 is the idle/ready value
# returned after a microwave operation finishes, so expose it as idle instead
# of leaving the entity as a raw unknown code.
MWO_STATE = {"0": "Standby", "1": "Setting", "2": "Running", "3": "Paused", "4": "Idle"}
MWO_COOK_TIME_STATE = {"0": "Standby", "1": "Running", "2": "Paused", "3": "Complete"}


@dataclass(frozen=True, kw_only=True)
class WhirlpoolApkSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Mapping[str, Any]], Any | None]
    cooking_only: bool = False
    microwave_only: bool = False


def _by_keys(*keys: str) -> Callable[[Mapping[str, Any]], Any | None]:
    return lambda flat: find_key(flat, keys)


def _legacy_attr(name: str) -> Callable[[Mapping[str, Any]], Any | None]:
    return lambda flat: attr_value(flat, name)


def _map_attr(name: str, mapping: Mapping[str, str]) -> Callable[[Mapping[str, Any]], Any | None]:
    def value(flat: Mapping[str, Any]) -> Any | None:
        raw = attr_value(flat, name)
        if raw is None:
            return None
        return mapping.get(str(raw), str(raw))
    return value


def _temp_tenths_attr(name: str) -> Callable[[Mapping[str, Any]], float | None]:
    def value(flat: Mapping[str, Any]) -> float | None:
        raw = attr_value(flat, name)
        if raw in (None, "", "0", 0):
            return None
        try:
            return int(raw) / 10
        except (TypeError, ValueError):
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None
    return value




def _int_legacy_attr(name: str) -> Callable[[Mapping[str, Any]], int | None]:
    def value(flat: Mapping[str, Any]) -> int | None:
        raw = attr_value(flat, name)
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    return value


def _mwo_state(flat: Mapping[str, Any]) -> Any | None:
    raw = attr_value(flat, "Mwo_OperationStatusState")
    if raw is None:
        return None
    return MWO_STATE.get(str(raw), f"State {raw}")

def _thing_time_remaining(flat: Mapping[str, Any]) -> Any | None:
    """Return remaining cycle time in minutes.

    Avoid generic keys like ``time`` because Whirlpool payloads contain epoch
    fields such as ``CREATED_AT`` and attribute ``updateTime`` values. Those
    previously matched the loose suffix search and caused timestamp overflow.
    """
    value = find_key(
        flat,
        (
            "timeRemaining",
            "remainingTime",
            "cycleTimeRemaining",
            "timeremaining",
            "cycleTime.time",
            "washer.cycleTime.time",
            "Cavity_TimeStatusEstTimeRemaining",
            "WashCavity_TimeStatusEstTimeRemaining",
        ),
    )
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return value
    if number < 0:
        return None
    # Anything above two weeks is almost certainly an epoch/update timestamp,
    # not an appliance duration.
    if number > 14 * 24 * 60 * 60:
        return None
    # ThingShield cycleTime.time is seconds. Legacy REST estimated remaining
    # time is commonly minutes. Keep small values as minutes.
    if number > 600:
        return number // 60
    return number


def _end_time(flat: Mapping[str, Any]) -> Any | None:
    minutes = _thing_time_remaining(flat)
    if minutes in (None, ""):
        return None
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return None
    if minutes < 0 or minutes > 14 * 24 * 60:
        return None
    try:
        return dt_util.utcnow() + timedelta(minutes=minutes)
    except OverflowError:
        return None


def _active_fault(flat: Mapping[str, Any]) -> Any | None:
    value = find_key(flat, ("activeFault", "faultCode", "errorCode", "alarmCode"))
    if value == "none":
        return None
    return value


def _generic_state(flat: Mapping[str, Any]) -> Any | None:
    raw = find_key(flat, ("state", "machineState", "applianceState", "Cavity_CycleStatusMachineState", "cavityState", "cycleStatus"))
    if raw is not None:
        return MACHINE_STATE.get(str(raw), raw)
    upper = attr_value(flat, "OvenUpperCavity_OpStatusState")
    lower = attr_value(flat, "OvenLowerCavity_OpStatusState")
    raw = upper if upper not in (None, "4") else lower
    return CAVITY_STATE.get(str(raw), raw) if raw is not None else None


def _generic_cook_mode(flat: Mapping[str, Any]) -> Any | None:
    raw = attr_value(flat, "OvenUpperCavity_CycleSetCommonMode")
    if raw in (None, "0"):
        raw = attr_value(flat, "OvenLowerCavity_CycleSetCommonMode")
    return COOK_MODE.get(str(raw), raw) if raw is not None else None


def _generic_current_temp(flat: Mapping[str, Any]) -> Any | None:
    for attr in ("OvenUpperCavity_DisplStatusDisplayTemp", "OvenLowerCavity_DisplStatusDisplayTemp"):
        value = _temp_tenths_attr(attr)(flat)
        if value is not None:
            return value
    return find_key(flat, ("currentTemperature", "temperature", "temp", "ovenTemperature", "currentTemp"))


def _generic_target_temp(flat: Mapping[str, Any]) -> Any | None:
    for attr in ("OvenUpperCavity_CycleSetTargetTemp", "OvenLowerCavity_CycleSetTargetTemp"):
        value = _temp_tenths_attr(attr)(flat)
        if value is not None:
            return value
    return find_key(flat, ("targetTemperature", "targetTemp", "setTemperature"))


SENSOR_DESCRIPTIONS: tuple[WhirlpoolApkSensorDescription, ...] = (
    WhirlpoolApkSensorDescription(key="state", translation_key="state", device_class=SensorDeviceClass.ENUM, value_fn=_generic_state),
    WhirlpoolApkSensorDescription(key="cycle", translation_key="cycle", value_fn=_by_keys("cycle", "cycleName", "currentCycle", "cycleLabel")),
    WhirlpoolApkSensorDescription(key="phase", translation_key="phase", value_fn=_by_keys("phase", "currentPhase", "cyclePhase", "cycleStep", "subState")),
    WhirlpoolApkSensorDescription(key="time_remaining", translation_key="time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="min", value_fn=_thing_time_remaining),
    WhirlpoolApkSensorDescription(key="end_time", translation_key="end_time", device_class=SensorDeviceClass.TIMESTAMP, value_fn=_end_time),
    WhirlpoolApkSensorDescription(key="current_temperature", translation_key="current_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_generic_current_temp),
    WhirlpoolApkSensorDescription(key="target_temperature", translation_key="target_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_generic_target_temp),
    WhirlpoolApkSensorDescription(key="humidity", translation_key="humidity", device_class=SensorDeviceClass.HUMIDITY, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=PERCENTAGE, value_fn=_by_keys("humidity", "currentHumidity")),
    WhirlpoolApkSensorDescription(key="filter_status", translation_key="filter_status", value_fn=_by_keys("filterStatus", "acFilterStatus")),
    WhirlpoolApkSensorDescription(key="fault_code", translation_key="fault_code", value_fn=_active_fault),
    WhirlpoolApkSensorDescription(key="cook_mode", translation_key="cook_mode", device_class=SensorDeviceClass.ENUM, options=list(COOK_MODE.values()), value_fn=_generic_cook_mode, cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cavity_state", translation_key="upper_cavity_state", device_class=SensorDeviceClass.ENUM, options=list(CAVITY_STATE.values()), value_fn=_map_attr("OvenUpperCavity_OpStatusState", CAVITY_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cavity_state", translation_key="lower_cavity_state", device_class=SensorDeviceClass.ENUM, options=list(CAVITY_STATE.values()), value_fn=_map_attr("OvenLowerCavity_OpStatusState", CAVITY_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cook_mode", translation_key="upper_cook_mode", device_class=SensorDeviceClass.ENUM, options=list(COOK_MODE.values()), value_fn=_map_attr("OvenUpperCavity_CycleSetCommonMode", COOK_MODE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cook_mode", translation_key="lower_cook_mode", device_class=SensorDeviceClass.ENUM, options=list(COOK_MODE.values()), value_fn=_map_attr("OvenLowerCavity_CycleSetCommonMode", COOK_MODE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_current_temperature", translation_key="upper_current_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenUpperCavity_DisplStatusDisplayTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_current_temperature", translation_key="lower_current_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenLowerCavity_DisplStatusDisplayTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_target_temperature", translation_key="upper_target_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenUpperCavity_CycleSetTargetTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_target_temperature", translation_key="lower_target_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenLowerCavity_CycleSetTargetTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cook_time_elapsed", translation_key="upper_cook_time_elapsed", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("OvenUpperCavity_TimeStatusCycleTimeElapsed"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cook_time_elapsed", translation_key="lower_cook_time_elapsed", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("OvenLowerCavity_TimeStatusCycleTimeElapsed"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="kitchen_timer_1_state", translation_key="kitchen_timer_1_state", device_class=SensorDeviceClass.ENUM, options=list(TIMER_STATE.values()), value_fn=_map_attr("KitchenTimer01_StatusState", TIMER_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="kitchen_timer_1_remaining", translation_key="kitchen_timer_1_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("KitchenTimer01_StatusTimeRemaining"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="microwave_state", translation_key="microwave_state", device_class=SensorDeviceClass.ENUM, options=list(MWO_STATE.values()), value_fn=_mwo_state, cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_time_state", translation_key="microwave_cook_time_state", device_class=SensorDeviceClass.ENUM, options=list(MWO_COOK_TIME_STATE.values()), value_fn=_map_attr("Mwo_OperationStatusCookTimeState", MWO_COOK_TIME_STATE), cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_time_remaining", translation_key="microwave_cook_time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_int_legacy_attr("Mwo_TimeStatusCookTimeRemaining"), cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_time_set", translation_key="microwave_cook_time_set", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_int_legacy_attr("Mwo_TimeSetCookTimeSet"), cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_power", translation_key="microwave_cook_power", value_fn=_int_legacy_attr("Mwo_CycleSetCookPower"), cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_current_temperature", translation_key="microwave_current_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("Mwo_DisplayStatusDisplayTemp"), cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_target_temperature", translation_key="microwave_target_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("Mwo_CycleSetTargetTemp"), cooking_only=True, microwave_only=True),
)


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        cooking = is_cooking_appliance(appliance)
        flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
        has_mwo = microwave_exists(flat)
        for desc in SENSOR_DESCRIPTIONS:
            if desc.cooking_only and not cooking:
                continue
            if cooking and desc.key in {
                "state",
                "cycle",
                "phase",
                "time_remaining",
                "end_time",
                "current_temperature",
                "target_temperature",
                "humidity",
                "filter_status",
                "cook_mode",
                "microwave_current_temperature",
                "microwave_target_temperature",
            }:
                # Minerva oven/microwave combo appliances already expose precise
                # oven/microwave entities below. The generic entities duplicate
                # those values or stay Unknown because the appliance is not a
                # washer/dryer/HVAC device.
                continue
            if desc.microwave_only and not has_mwo:
                continue
            if desc.key.startswith("upper_") and not oven_cavity_exists(flat, "upper"):
                continue
            if desc.key.startswith("lower_") and not oven_cavity_exists(flat, "lower"):
                continue
            entities.append(WhirlpoolApkSensor(coordinator, appliance, desc))
        if entry.data.get(CONF_EXPOSE_RAW_SENSORS, True):
            entities.append(WhirlpoolRawStatusSensor(coordinator, appliance))
    async_add_entities(entities)


class WhirlpoolApkSensor(WhirlpoolApkEntity, SensorEntity):
    entity_description: WhirlpoolApkSensorDescription

    def __init__(self, coordinator, appliance: Mapping[str, Any], description: WhirlpoolApkSensorDescription) -> None:
        super().__init__(coordinator, appliance, description.key)
        self.entity_description = description
        self._attr_name = entity_name_from_key(description.translation_key or description.key, appliance)

    @property
    def native_unit_of_measurement(self) -> str | None:
        if self.entity_description.device_class == SensorDeviceClass.TEMPERATURE:
            return self.temperature_unit
        return self.entity_description.native_unit_of_measurement

    @property
    def native_value(self) -> Any | None:
        value = self.entity_description.value_fn(self.flat_status)
        if self.entity_description.device_class == SensorDeviceClass.TEMPERATURE and isinstance(value, (int, float)):
            return celsius_to_unit(value, self.temperature_unit)
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)[:255]
        if isinstance(value, str) and self.entity_description.device_class == SensorDeviceClass.ENUM:
            return value.replace("_", " ").title()
        return value


class WhirlpoolRawStatusSensor(WhirlpoolApkEntity, SensorEntity):
    _attr_entity_registry_enabled_default = False
    _attr_translation_key = "raw_status"

    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "raw_status")
        self._attr_name = entity_name_from_key("raw_status")

    @property
    def native_value(self) -> str | None:
        if isinstance(self.status, Mapping) and self.status.get("error"):
            return "error"
        return "ok" if self.status else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"status": self.status, "appliance": self.appliance}
