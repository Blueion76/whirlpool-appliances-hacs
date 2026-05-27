"""Sensors for Whirlpool Appliances integration."""
from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import WhirlpoolApkConfigEntry
from .api import appliance_said, appliance_name
from .const import CONF_EXPOSE_RAW_SENSORS
from .entity import WhirlpoolApkEntity, attr_value, celsius_to_unit, entity_name_from_key, find_key, flatten, is_aircon_appliance, is_cooktop_appliance, is_cooking_appliance, is_dishwasher_appliance, is_laundry_appliance, is_refrigeration_appliance, microwave_exists, oven_cavity_exists

CAVITY_STATE = {"0": "Standby", "1": "Preheating", "2": "Cooking", "4": "Not Present"}
COOK_MODE = {"0": "Standby", "2": "Bake", "6": "Convection Bake", "8": "Broil", "9": "Convection Broil", "16": "Convection Roast", "24": "Keep Warm", "41": "Air Fry"}
OVEN_COOK_TIME_STATE = {"0": "Idle", "1": "Running", "2": "Paused", "3": "Complete"}
TIMER_STATE = {"0": "Standby", "1": "Running", "3": "Completed"}
MWO_STATE = {"0": "Standby", "1": "Setting", "2": "Running", "3": "Paused", "4": "Idle", "6": "Sense", "8": "Wait Add Food", "9": "Wait Turn Food", "10": "Wait Stir Food"}
MWO_COOK_TIME_STATE = {"0": "Standby", "1": "Running", "2": "Paused", "3": "Complete"}
MACHINE_STATE = {"0": "Standby", "1": "Setting", "2": "Delay Countdown", "3": "Delay Paused", "6": "Paused", "7": "Running", "9": "Exception", "10": "Complete", "17": "Error"}
AC_MODE = {"0": "Off", "1": "Cool", "2": "Heat", "3": "Fan", "4": "Dry", "5": "Auto"}

@dataclass(frozen=True, kw_only=True)
class WhirlpoolApkSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Mapping[str, Any]], Any | None]
    cooking_only: bool = False
    microwave_only: bool = False
    laundry_only: bool = False
    refrigeration_only: bool = False
    aircon_only: bool = False
    dishwasher_only: bool = False
    cooktop_only: bool = False
    hood_only: bool = False


def _by_keys(*keys: str) -> Callable[[Mapping[str, Any]], Any | None]:
    return lambda flat: find_key(flat, keys)


def _legacy_attr(name: str) -> Callable[[Mapping[str, Any]], Any | None]:
    return lambda flat: attr_value(flat, name)


def _map_attr(name: str, mapping: Mapping[str, str]) -> Callable[[Mapping[str, Any]], Any | None]:
    def value(flat: Mapping[str, Any]) -> Any | None:
        raw = attr_value(flat, name)
        return mapping.get(str(raw), str(raw)) if raw is not None else None
    return value


def _int_value(raw: Any) -> int | None:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None


def _int_legacy_attr(name: str) -> Callable[[Mapping[str, Any]], int | None]:
    return lambda flat: _int_value(attr_value(flat, name))


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


def _attr_update_time(flat: Mapping[str, Any], attr: str) -> int | None:
    return _int_value(find_key(flat, (f"attributes.{attr}.updateTime", f"{attr}.updateTime", f"{attr}_updateTime")))


def _oven_prefix(cavity: str) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def _oven_is_active(flat: Mapping[str, Any], cavity: str) -> bool:
    return str(attr_value(flat, f"{_oven_prefix(cavity)}_OpStatusState") or "") in {"1", "2"}


def _oven_elapsed_seconds(flat: Mapping[str, Any], cavity: str) -> int | None:
    prefix = _oven_prefix(cavity)
    raw = _int_value(attr_value(flat, f"{prefix}_TimeStatusCycleTimeElapsed"))
    if raw and raw > 0:
        return raw
    if not _oven_is_active(flat, cavity):
        return None
    start_ms = _attr_update_time(flat, f"{prefix}_OpSetOperations") or _attr_update_time(flat, f"{prefix}_OpStatusState")
    if not start_ms:
        return None
    elapsed = int((time.time() * 1000 - start_ms) / 1000)
    return elapsed if 0 <= elapsed <= 14 * 24 * 60 * 60 else None


def _oven_cook_mode(flat: Mapping[str, Any], cavity: str) -> Any | None:
    prefix = _oven_prefix(cavity)
    raw = attr_value(flat, f"{prefix}_CycleSetCommonMode")
    state = str(attr_value(flat, f"{prefix}_OpStatusState") or "")
    if raw in (None, "0", 0) and state in {"1", "2"}:
        return None
    return COOK_MODE.get(str(raw), raw) if raw is not None else None


def _mwo_cook_time_set_seconds(flat: Mapping[str, Any]) -> int | None:
    value = _int_value(attr_value(flat, "Mwo_TimeSetCookTimeSet"))
    return value if value and value > 0 else None


def _mwo_is_active(flat: Mapping[str, Any]) -> bool:
    state = str(attr_value(flat, "Mwo_OperationStatusState") or "")
    if state in {"2", "3", "6", "8", "9", "10"}:
        return True
    operation = str(attr_value(flat, "Mwo_OperationSetOperations") or "")
    start_ms = _attr_update_time(flat, "Mwo_OperationSetOperations")
    set_seconds = _mwo_cook_time_set_seconds(flat)
    if operation in {"2", "3", "4"} and start_ms and set_seconds:
        elapsed = int((time.time() * 1000 - start_ms) / 1000)
        return 0 <= elapsed <= set_seconds + 300
    return False


def _mwo_elapsed_seconds(flat: Mapping[str, Any]) -> int | None:
    raw = _int_value(attr_value(flat, "Mwo_TimeStatusCycleTimeElapsed"))
    if raw and raw > 0:
        return raw
    set_seconds = _mwo_cook_time_set_seconds(flat)
    start_ms = _attr_update_time(flat, "Mwo_OperationSetOperations")
    operation = str(attr_value(flat, "Mwo_OperationSetOperations") or "")
    if operation not in {"2", "3", "4"} or not start_ms or not set_seconds:
        return None
    elapsed = int((time.time() * 1000 - start_ms) / 1000)
    return min(elapsed, set_seconds) if 0 <= elapsed <= set_seconds + 300 else None


def _mwo_remaining_seconds(flat: Mapping[str, Any]) -> int | None:
    raw = _int_value(attr_value(flat, "Mwo_TimeStatusCookTimeRemaining"))
    if raw and raw > 0:
        return raw
    set_seconds = _mwo_cook_time_set_seconds(flat)
    elapsed = _mwo_elapsed_seconds(flat)
    return max(set_seconds - elapsed, 0) if set_seconds is not None and elapsed is not None else None


def _mwo_cook_power(flat: Mapping[str, Any]) -> int | None:
    raw = _int_value(attr_value(flat, "Mwo_CycleSetCookPower"))
    if raw and raw > 0:
        return raw
    if str(attr_value(flat, "Mwo_CycleSetCookPower") or "") == "0" and (_attr_update_time(flat, "Mwo_CycleSetCookPower") or 0) <= 1540000000000:
        return 100 if _mwo_is_active(flat) else None
    return raw


def _mwo_state(flat: Mapping[str, Any]) -> str | None:
    raw = attr_value(flat, "Mwo_OperationStatusState")
    return MWO_STATE.get(str(raw), f"State {raw}") if raw is not None else None


def _normalize_fault(value: Any) -> str | None:
    if value in (None, "", 0, False):
        return "Clear"
    if isinstance(value, str) and value.strip().lower() in {"0", "false", "none", "no", "clear", "ok", "normal", "no_fault", "no fault", "no_error", "no error"}:
        return "Clear"
    return str(value).strip()


def _active_fault(flat: Mapping[str, Any]) -> str | None:
    return _normalize_fault(find_key(flat, ("activeFault", "faultCode", "errorCode", "alarmCode", "Sys_AlertStatusCustomerFaultCode", "Sys_AlertStatusCustomerFaultCodeNotification")))


def _thing_time_remaining(flat: Mapping[str, Any]) -> int | None:
    value = _int_value(find_key(flat, ("timeRemaining", "remainingTime", "cycleTimeRemaining", "timeremaining", "cycleTime.time", "washer.cycleTime.time")))
    if value is None or value < 0 or value > 14 * 24 * 60 * 60:
        return None
    return value // 60 if value > 600 else value


def _end_time(flat: Mapping[str, Any]) -> Any | None:
    minutes = _thing_time_remaining(flat)
    return dt_util.utcnow() + timedelta(minutes=minutes) if minutes is not None else None


def _laundry_state(flat: Mapping[str, Any]) -> str | None:
    raw = find_key(flat, ("washer.applianceState", "dryer.applianceState", "applianceState", "machineState"))
    if raw is None:
        return None
    return MACHINE_STATE.get(str(raw), str(raw).replace("_", " ").title())


def _is_laundry_status(flat: Mapping[str, Any]) -> bool:
    return any(find_key(flat, (key,)) is not None for key in ("washer.applianceState", "dryer.applianceState", "applianceState", "washer.cycleName", "dryer.cycleName", "doorLockStatus", "cleanWasher"))


def _temperature_value(flat: Mapping[str, Any], *keys: str) -> float | None:
    raw = find_key(flat, keys)
    try:
        value = float(raw) if raw not in (None, "", "0") else None
    except (TypeError, ValueError):
        return None
    return value / 10 if value and abs(value) > 200 else value


def _mapped_key(mapping: Mapping[str, str], *keys: str) -> Callable[[Mapping[str, Any]], str | None]:
    def value(flat: Mapping[str, Any]) -> str | None:
        raw = find_key(flat, keys)
        return mapping.get(str(raw), str(raw).replace("_", " ").title()) if raw not in (None, "") else None
    return value


def _cooktop_zone_value(flat: Mapping[str, Any], zone: int, *names: str) -> Any | None:
    candidates: list[str] = []
    for name in names:
        candidates.extend(
            (
                f"zones.{zone}.{name}",
                f"zone{zone}.{name}",
                f"cooktop.zones.{zone}.{name}",
                f"cooktop.zone{zone}.{name}",
                f"cooktopZone{zone}.{name}",
                f"cooktopZone{zone}{name[0].upper()}{name[1:]}",
                f"Cooktop_Zone{zone}_{name}",
                f"zone_{zone}_{name}",
            )
        )
    return find_key(flat, tuple(candidates))


def _hood_value(flat: Mapping[str, Any], *names: str) -> Any | None:
    candidates: list[str] = []
    for name in names:
        candidates.extend(
            (
                name,
                f"hood.{name}",
                f"hoodFan.{name}",
                f"hoodLight.{name}",
                f"ventilation.{name}",
                f"vent.{name}",
                f"Hood_{name}",
            )
        )
    return find_key(flat, tuple(candidates))



def _has_hood_status(flat: Mapping[str, Any]) -> bool:
    """Return true only when this appliance status actually exposes hood/vent fields."""
    return any(
        _hood_value(flat, *names) not in (None, "")
        for names in (
            ("fanSpeed", "hoodFan", "hoodFanSpeed", "fanLevel"),
            ("hoodLight", "light", "lightOn", "HoodLight"),
            ("hoodLightColor", "lightColor", "color"),
            ("hoodState", "ventilationState"),
        )
    )



SENSOR_DESCRIPTIONS: tuple[WhirlpoolApkSensorDescription, ...] = (
    WhirlpoolApkSensorDescription(key="state", translation_key="state", device_class=SensorDeviceClass.ENUM, value_fn=_mapped_key(MACHINE_STATE, "state", "machineState", "applianceState")),
    WhirlpoolApkSensorDescription(key="cycle", translation_key="cycle", value_fn=_by_keys("cycle", "cycleName", "currentCycle", "cycleLabel")),
    WhirlpoolApkSensorDescription(key="phase", translation_key="phase", value_fn=_by_keys("phase", "currentPhase", "cyclePhase", "cycleStep", "subState")),
    WhirlpoolApkSensorDescription(key="time_remaining", translation_key="time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="min", value_fn=_thing_time_remaining),
    WhirlpoolApkSensorDescription(key="end_time", translation_key="end_time", device_class=SensorDeviceClass.TIMESTAMP, value_fn=_end_time),
    WhirlpoolApkSensorDescription(key="current_temperature", translation_key="current_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat: _temperature_value(flat, "currentTemperature", "temperature", "temp")),
    WhirlpoolApkSensorDescription(key="target_temperature", translation_key="target_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat: _temperature_value(flat, "targetTemperature", "targetTemp", "setTemperature")),
    WhirlpoolApkSensorDescription(key="humidity", translation_key="humidity", device_class=SensorDeviceClass.HUMIDITY, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=PERCENTAGE, value_fn=_by_keys("humidity", "currentHumidity")),
    WhirlpoolApkSensorDescription(key="filter_status", translation_key="filter_status", value_fn=_by_keys("filterStatus", "acFilterStatus")),
    WhirlpoolApkSensorDescription(key="laundry_state", translation_key="laundry_state", icon="mdi:washing-machine", device_class=SensorDeviceClass.ENUM, options=list(MACHINE_STATE.values()), value_fn=_laundry_state, laundry_only=True),
    WhirlpoolApkSensorDescription(key="laundry_cycle", translation_key="laundry_cycle", icon="mdi:washing-machine", value_fn=_by_keys("washer.cycleName", "dryer.cycleName", "cycleName", "cycle"), laundry_only=True),
    WhirlpoolApkSensorDescription(key="laundry_phase", translation_key="laundry_phase", icon="mdi:progress-clock", value_fn=_by_keys("washer.currentPhase", "dryer.currentPhase", "currentPhase", "cyclePhase"), laundry_only=True),
    WhirlpoolApkSensorDescription(key="laundry_time_remaining", translation_key="laundry_time_remaining", icon="mdi:timer-outline", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="min", value_fn=_thing_time_remaining, laundry_only=True),
    WhirlpoolApkSensorDescription(key="laundry_end_time", translation_key="laundry_end_time", icon="mdi:progress-clock", device_class=SensorDeviceClass.TIMESTAMP, value_fn=_end_time, laundry_only=True),
    WhirlpoolApkSensorDescription(key="dishwasher_state", translation_key="dishwasher_state", icon="mdi:dishwasher", device_class=SensorDeviceClass.ENUM, options=list(MACHINE_STATE.values()), value_fn=_mapped_key(MACHINE_STATE, "dishwasher.applianceState", "machineState"), dishwasher_only=True),
    WhirlpoolApkSensorDescription(key="dishwasher_time_remaining", translation_key="dishwasher_time_remaining", icon="mdi:timer-outline", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_by_keys("dishwasher.cycleTime.time", "cycleTime.time", "timeRemaining"), dishwasher_only=True),
    WhirlpoolApkSensorDescription(key="refrigerator_temperature", translation_key="refrigerator_temperature", icon="mdi:fridge", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat: _temperature_value(flat, "refrigeratorTemperature", "refrigerator.currentTemperature"), refrigeration_only=True),
    WhirlpoolApkSensorDescription(key="freezer_temperature", translation_key="freezer_temperature", icon="mdi:snowflake", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat: _temperature_value(flat, "freezerTemperature", "freezer.currentTemperature"), refrigeration_only=True),
    WhirlpoolApkSensorDescription(key="ac_mode", translation_key="ac_mode", icon="mdi:air-conditioner", device_class=SensorDeviceClass.ENUM, options=list(AC_MODE.values()), value_fn=_mapped_key(AC_MODE, "ac.mode", "mode", "operationMode", "airconMode"), aircon_only=True),
    WhirlpoolApkSensorDescription(key="ac_fan_speed", translation_key="ac_fan_speed", icon="mdi:fan", value_fn=_by_keys("fanSpeed", "fan_speed", "ac.fanSpeed", "airconFanSpeed"), aircon_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_state", translation_key="cooktop_state", icon="mdi:stove", value_fn=_by_keys("cooktopState", "cooktop.state", "machineState", "applianceState"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="hood_fan_speed", translation_key="hood_fan_speed", icon="mdi:fan", value_fn=lambda flat: _hood_value(flat, "fanSpeed", "hoodFan", "hoodFanSpeed", "fanLevel"), hood_only=True),
    WhirlpoolApkSensorDescription(key="hood_light", translation_key="hood_light", icon="mdi:lightbulb", value_fn=lambda flat: _hood_value(flat, "hoodLight", "light", "lightOn", "HoodLight"), hood_only=True),
    WhirlpoolApkSensorDescription(key="hood_light_color", translation_key="hood_light_color", icon="mdi:palette", value_fn=lambda flat: _hood_value(flat, "hoodLightColor", "lightColor", "color"), hood_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_1_state", translation_key="cooktop_zone_1_state", icon="mdi:stove", value_fn=lambda flat, zone=1: _cooktop_zone_value(flat, zone, "state", "zoneState", "status"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_1_power", translation_key="cooktop_zone_1_power", icon="mdi:lightning-bolt", value_fn=lambda flat, zone=1: _cooktop_zone_value(flat, zone, "power", "powerLevel", "powerlevel", "level"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_1_temperature", translation_key="cooktop_zone_1_temperature", icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat, zone=1: _temperature_value(flat, f"zone1.temperature", f"cooktop.zone1.temperature", f"cooktopZone1.temperature"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_2_state", translation_key="cooktop_zone_2_state", icon="mdi:stove", value_fn=lambda flat, zone=2: _cooktop_zone_value(flat, zone, "state", "zoneState", "status"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_2_power", translation_key="cooktop_zone_2_power", icon="mdi:lightning-bolt", value_fn=lambda flat, zone=2: _cooktop_zone_value(flat, zone, "power", "powerLevel", "powerlevel", "level"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_2_temperature", translation_key="cooktop_zone_2_temperature", icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat, zone=2: _temperature_value(flat, f"zone2.temperature", f"cooktop.zone2.temperature", f"cooktopZone2.temperature"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_3_state", translation_key="cooktop_zone_3_state", icon="mdi:stove", value_fn=lambda flat, zone=3: _cooktop_zone_value(flat, zone, "state", "zoneState", "status"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_3_power", translation_key="cooktop_zone_3_power", icon="mdi:lightning-bolt", value_fn=lambda flat, zone=3: _cooktop_zone_value(flat, zone, "power", "powerLevel", "powerlevel", "level"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_3_temperature", translation_key="cooktop_zone_3_temperature", icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat, zone=3: _temperature_value(flat, f"zone3.temperature", f"cooktop.zone3.temperature", f"cooktopZone3.temperature"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_4_state", translation_key="cooktop_zone_4_state", icon="mdi:stove", value_fn=lambda flat, zone=4: _cooktop_zone_value(flat, zone, "state", "zoneState", "status"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_4_power", translation_key="cooktop_zone_4_power", icon="mdi:lightning-bolt", value_fn=lambda flat, zone=4: _cooktop_zone_value(flat, zone, "power", "powerLevel", "powerlevel", "level"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_4_temperature", translation_key="cooktop_zone_4_temperature", icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat, zone=4: _temperature_value(flat, f"zone4.temperature", f"cooktop.zone4.temperature", f"cooktopZone4.temperature"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_5_state", translation_key="cooktop_zone_5_state", icon="mdi:stove", value_fn=lambda flat, zone=5: _cooktop_zone_value(flat, zone, "state", "zoneState", "status"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_5_power", translation_key="cooktop_zone_5_power", icon="mdi:lightning-bolt", value_fn=lambda flat, zone=5: _cooktop_zone_value(flat, zone, "power", "powerLevel", "powerlevel", "level"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="cooktop_zone_5_temperature", translation_key="cooktop_zone_5_temperature", icon="mdi:thermometer", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=lambda flat, zone=5: _temperature_value(flat, f"zone5.temperature", f"cooktop.zone5.temperature", f"cooktopZone5.temperature"), cooktop_only=True),
    WhirlpoolApkSensorDescription(key="fault_code", translation_key="fault_code", icon="mdi:alert", value_fn=_active_fault),
    WhirlpoolApkSensorDescription(key="cook_mode", translation_key="cook_mode", icon="mdi:chef-hat", device_class=SensorDeviceClass.ENUM, options=list(COOK_MODE.values()), value_fn=lambda flat: _oven_cook_mode(flat, "upper") or _oven_cook_mode(flat, "lower"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cavity_state", translation_key="upper_cavity_state", icon="mdi:stove", device_class=SensorDeviceClass.ENUM, options=list(CAVITY_STATE.values()), value_fn=_map_attr("OvenUpperCavity_OpStatusState", CAVITY_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cavity_state", translation_key="lower_cavity_state", icon="mdi:stove", device_class=SensorDeviceClass.ENUM, options=list(CAVITY_STATE.values()), value_fn=_map_attr("OvenLowerCavity_OpStatusState", CAVITY_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cook_mode", translation_key="upper_cook_mode", icon="mdi:chef-hat", device_class=SensorDeviceClass.ENUM, options=list(COOK_MODE.values()), value_fn=lambda flat: _oven_cook_mode(flat, "upper"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cook_mode", translation_key="lower_cook_mode", icon="mdi:chef-hat", device_class=SensorDeviceClass.ENUM, options=list(COOK_MODE.values()), value_fn=lambda flat: _oven_cook_mode(flat, "lower"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_current_temperature", translation_key="upper_current_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenUpperCavity_DisplStatusDisplayTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_current_temperature", translation_key="lower_current_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenLowerCavity_DisplStatusDisplayTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_target_temperature", translation_key="upper_target_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenUpperCavity_CycleSetTargetTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_target_temperature", translation_key="lower_target_temperature", device_class=SensorDeviceClass.TEMPERATURE, state_class=SensorStateClass.MEASUREMENT, native_unit_of_measurement=UnitOfTemperature.CELSIUS, value_fn=_temp_tenths_attr("OvenLowerCavity_CycleSetTargetTemp"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cook_time_elapsed", translation_key="upper_cook_time_elapsed", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=lambda flat: _oven_elapsed_seconds(flat, "upper"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cook_time_elapsed", translation_key="lower_cook_time_elapsed", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=lambda flat: _oven_elapsed_seconds(flat, "lower"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cook_time_remaining", translation_key="upper_cook_time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("OvenUpperCavity_TimeStatusCookTimeRemaining"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cook_time_remaining", translation_key="lower_cook_time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("OvenLowerCavity_TimeStatusCookTimeRemaining"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_delay_time_remaining", translation_key="upper_delay_time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("OvenUpperCavity_TimeStatusDelayTimeRemaining"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_delay_time_remaining", translation_key="lower_delay_time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("OvenLowerCavity_TimeStatusDelayTimeRemaining"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="upper_cook_time_state", translation_key="upper_cook_time_state", icon="mdi:timer-sand", device_class=SensorDeviceClass.ENUM, options=list(OVEN_COOK_TIME_STATE.values()), value_fn=_map_attr("OvenUpperCavity_OpStatusCookTimeState", OVEN_COOK_TIME_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="lower_cook_time_state", translation_key="lower_cook_time_state", icon="mdi:timer-sand", device_class=SensorDeviceClass.ENUM, options=list(OVEN_COOK_TIME_STATE.values()), value_fn=_map_attr("OvenLowerCavity_OpStatusCookTimeState", OVEN_COOK_TIME_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="kitchen_timer_1_state", translation_key="kitchen_timer_1_state", icon="mdi:timer-off", device_class=SensorDeviceClass.ENUM, options=list(TIMER_STATE.values()), value_fn=_map_attr("KitchenTimer01_StatusState", TIMER_STATE), cooking_only=True),
    WhirlpoolApkSensorDescription(key="kitchen_timer_1_remaining", translation_key="kitchen_timer_1_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_legacy_attr("KitchenTimer01_StatusTimeRemaining"), cooking_only=True),
    WhirlpoolApkSensorDescription(key="microwave_state", translation_key="microwave_state", icon="mdi:microwave-off", device_class=SensorDeviceClass.ENUM, options=list(MWO_STATE.values()), value_fn=_mwo_state, cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_time_state", translation_key="microwave_cook_time_state", icon="mdi:timer-sand", device_class=SensorDeviceClass.ENUM, options=list(MWO_COOK_TIME_STATE.values()), value_fn=_map_attr("Mwo_OperationStatusCookTimeState", MWO_COOK_TIME_STATE), cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_time_remaining", translation_key="microwave_cook_time_remaining", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_mwo_remaining_seconds, cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_time_elapsed", translation_key="microwave_cook_time_elapsed", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_mwo_elapsed_seconds, cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_time_set", translation_key="microwave_cook_time_set", icon="mdi:timer", device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_int_legacy_attr("Mwo_TimeSetCookTimeSet"), cooking_only=True, microwave_only=True),
    WhirlpoolApkSensorDescription(key="microwave_cook_power", translation_key="microwave_cook_power", icon="mdi:lightning-bolt", native_unit_of_measurement=PERCENTAGE, value_fn=_mwo_cook_power, cooking_only=True, microwave_only=True),
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
        laundry = is_laundry_appliance(appliance) or _is_laundry_status(flat)
        refrigeration = is_refrigeration_appliance(appliance)
        aircon = is_aircon_appliance(appliance)
        dishwasher = is_dishwasher_appliance(appliance)
        cooktop = is_cooktop_appliance(appliance)
        has_hood = _has_hood_status(flat)
        for desc in SENSOR_DESCRIPTIONS:
            if desc.cooking_only and not cooking:
                continue
            if desc.laundry_only and not laundry:
                continue
            if desc.refrigeration_only and not refrigeration:
                continue
            if desc.aircon_only and not aircon:
                continue
            if desc.dishwasher_only and not dishwasher:
                continue
            if desc.cooktop_only and not cooktop:
                continue
            if desc.hood_only and not has_hood:
                continue
            if (laundry or dishwasher) and desc.key in {"state", "cycle", "phase", "time_remaining", "end_time"}:
                continue
            if cooking and desc.key in {"state", "cycle", "phase", "time_remaining", "end_time", "current_temperature", "target_temperature", "humidity", "filter_status", "cook_mode", "microwave_current_temperature", "microwave_target_temperature"}:
                continue
            if desc.microwave_only and not has_mwo:
                continue
            if desc.key.startswith("upper_") and not oven_cavity_exists(flat, "upper"):
                continue
            if desc.key.startswith("lower_") and not oven_cavity_exists(flat, "lower"):
                continue
            entities.append(WhirlpoolApkSensor(coordinator, appliance, desc))
        if bool(appliance.get("thingShield")) or str(appliance.get("source") or "").upper() == "TS_SAID":
            entities.extend(WhirlpoolThingShieldSensor(coordinator, appliance, key) for key in THINGSHIELD_SENSOR_KEYS)
        if entry.options.get(CONF_EXPOSE_RAW_SENSORS, entry.data.get(CONF_EXPOSE_RAW_SENSORS, True)):
            entities.append(WhirlpoolRawStatusSensor(coordinator, appliance))
    # Accessory/probe entities disabled: this account reports no accessories and Whirlpool rejects the accessory endpoint.
    async_add_entities(entities)


class WhirlpoolApkSensor(WhirlpoolApkEntity, SensorEntity):
    entity_description: WhirlpoolApkSensorDescription
    def __init__(self, coordinator, appliance: Mapping[str, Any], description: WhirlpoolApkSensorDescription) -> None:
        super().__init__(coordinator, appliance, description.key)
        self.entity_description = description
        self._attr_name = entity_name_from_key(description.translation_key or description.key, appliance)

    @property
    def native_unit_of_measurement(self) -> str | None:
        return self.temperature_unit if self.entity_description.device_class == SensorDeviceClass.TEMPERATURE else self.entity_description.native_unit_of_measurement

    @property
    def icon(self) -> str | None:
        if self.entity_description.key == "microwave_state":
            return "mdi:microwave" if self.native_value in {"Setting", "Running", "Paused", "Sense", "Wait Add Food", "Wait Turn Food", "Wait Stir Food"} else "mdi:microwave-off"
        if self.entity_description.key == "kitchen_timer_1_state":
            return "mdi:timer-off" if self.native_value in {None, "Standby"} else "mdi:timer"
        return self.entity_description.icon

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
    _attr_icon = "mdi:list-status"
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


THINGSHIELD_SENSOR_KEYS = (
    "ts_machine_state",
    "ts_cycle",
    "ts_phase",
    "ts_fault_code",
    "ts_cavity_state",
    "ts_cooktop_zone_state",
    "ts_refrigerator_state",
    "ts_hood_state",
    "ts_hood_fan",
    "ts_hood_light",
)

TS_SENSOR_CANDIDATES = {
    "ts_machine_state": ("machineState", "applianceState", "state", "cavityStatus.machineState"),
    "ts_cycle": ("cycle", "cycleName", "currentCycle", "cycleLabel"),
    "ts_phase": ("phase", "currentPhase", "cyclePhase", "cycleStep", "subState"),
    "ts_fault_code": ("faultCode", "fault", "errorCode", "activeFault", "alarmCode"),
    "ts_cavity_state": ("cavityState", "cavityStatus.state", "ovenState"),
    "ts_cooktop_zone_state": ("cooktopZoneState", "zoneState", "cooktop.zoneState"),
    "ts_refrigerator_state": ("refrigeratorState", "refrigerationState", "fridgeState"),
    "ts_hood_state": ("hoodState", "hood.state", "ventilationState"),
    "ts_hood_fan": ("hoodFan", "hoodFanSpeed", "fanSpeed", "ventilation.fanSpeed"),
    "ts_hood_light": ("hoodLight", "hoodLightOn", "hood.light", "lightOn"),
}

ACCESSORY_SENSOR_DEFINITIONS = {
    "probe_current_temperature": {
        "name": "Probe current temperature",
        "keys": ("probeCurrentTemp", "currentTemperature", "probe.currentTemperature", "foodTemperature", "meatProbe.temperature", "meatProbeTemp"),
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
    },
    "probe_ambient_temperature": {
        "name": "Probe ambient temperature",
        "keys": ("probeAmbientTemp", "ambientTemperature", "probe.ambientTemperature"),
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
    },
    "probe_middle_temperature": {
        "name": "Probe middle temperature",
        "keys": ("probeMiddleTemp", "middleTemperature", "probe.middleTemperature"),
        "device_class": SensorDeviceClass.TEMPERATURE,
        "unit": UnitOfTemperature.CELSIUS,
    },
    "probe_battery": {
        "name": "Probe battery",
        "keys": ("probeBattery", "probeBatteryLevel", "probeBatteryPercentage", "battery", "batteryLevel"),
        "device_class": SensorDeviceClass.BATTERY,
        "unit": PERCENTAGE,
    },
    "probe_signal_strength": {
        "name": "Probe signal strength",
        "keys": ("probeSignalStrength", "signalStrength", "rssi"),
        "unit": PERCENTAGE,
    },
    "probe_status": {
        "name": "Probe status",
        "keys": ("probeStatus", "status", "probeStatusDetails", "meatProbe.status"),
    },
    "probe_firmware_version": {
        "name": "Probe firmware version",
        "keys": ("probeFirmwareVersion", "firmwareVersion", "softwareVersion"),
    },
    "probe_hardware_version": {
        "name": "Probe hardware version",
        "keys": ("probeHardwareVersion", "hardwareVersion"),
    },
    "probe_serial_number": {
        "name": "Probe serial number",
        "keys": ("probeSerialNumber", "serialNumber", "serial", "id"),
    },
    "probe_mac_address": {
        "name": "Probe MAC address",
        "keys": ("probeMacAddress", "macAddress", "mac"),
    },
    "probe_connected": {
        "name": "Probe connected",
        "keys": ("probeConnected", "connected", "isConnected"),
    },
    "probe_inserted": {
        "name": "Probe inserted",
        "keys": ("probeInserted", "inserted", "isInserted"),
    },
}


class WhirlpoolThingShieldSensor(WhirlpoolApkEntity, SensorEntity):
    """Diagnostic sensor for ThingShield appliance state payloads."""

    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator, appliance: Mapping[str, Any], key: str) -> None:
        super().__init__(coordinator, appliance, key)
        self._key = key
        self._attr_translation_key = key
        self._attr_name = entity_name_from_key(key, appliance)

    @property
    def native_value(self) -> Any | None:
        value = find_key(self.flat_status, TS_SENSOR_CANDIDATES[self._key])
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)[:255]
        return str(value).replace("_", " ").title() if isinstance(value, str) else value


async def _async_accessory_entities(coordinator) -> list[SensorEntity]:
    """Create accessory/probe entities from the account accessory list."""
    try:
        payload = await coordinator.client.request("GET", "/api/v1/accessory")
    except Exception:  # noqa: BLE001 - accessory endpoint is optional per account/region
        return []
    entities: list[SensorEntity] = []
    for accessory in _coerce_accessories(payload):
        serial = _accessory_serial(accessory)
        if not serial:
            continue
        for key, definition in ACCESSORY_SENSOR_DEFINITIONS.items():
            entities.append(WhirlpoolAccessorySensor(coordinator, serial, accessory, key, definition))
    return entities


def _coerce_accessories(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("accessories", "items", "data", "results", "devices"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        return [payload]
    return []


def _accessory_serial(accessory: Mapping[str, Any]) -> str | None:
    flat = flatten(accessory)
    for key in ("serialNumber", "probeSerialNumber", "serial", "id", "accessoryId", "macAddress"):
        value = find_key(flat, (key,))
        if value not in (None, "", 0):
            return str(value)
    return None


class WhirlpoolAccessorySensor(SensorEntity):
    """Sensor backed by Whirlpool accessory/probe status."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, serial: str, initial: Mapping[str, Any], key: str, definition: Mapping[str, Any]) -> None:
        self.coordinator = coordinator
        self.serial = serial
        self._key = key
        self._definition = definition
        self._payload: Mapping[str, Any] = initial
        self._attr_unique_id = f"whirlpool_accessory_{serial}_{key}"
        self._attr_name = str(definition["name"])
        self._attr_icon = "mdi:thermometer-probe" if key.startswith("probe_") else None
        self._attr_device_class = definition.get("device_class")
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_state_class = SensorStateClass.MEASUREMENT if self._attr_device_class in {SensorDeviceClass.TEMPERATURE, SensorDeviceClass.BATTERY} else None

    @property
    def device_info(self):
        return {
            "identifiers": {("whirlpool_appliances", f"accessory_{self.serial}")},
            "name": _accessory_name(self._payload, self.serial),
            "manufacturer": "Whirlpool",
            "model": _first_flat(self._payload, "model", "modelNumber", "type", "accessoryType"),
            "sw_version": _first_flat(self._payload, "firmwareVersion", "probeFirmwareVersion", "softwareVersion"),
        }

    @property
    def native_value(self) -> Any | None:
        flat = flatten(self._payload)
        value = _first_flat(flat, *self._definition["keys"])
        if self._attr_device_class == SensorDeviceClass.TEMPERATURE:
            return _normalize_temperature(value)
        if self._attr_device_class == SensorDeviceClass.BATTERY:
            return _normalize_number(value)
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True)[:255]
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"serial_number": self.serial, "status": self._payload}

    async def async_update(self) -> None:
        try:
            payload = await self.coordinator.client.request("GET", f"/api/v1/accessory/{self.serial}")
        except Exception:  # noqa: BLE001 - keep last known accessory state
            return
        if isinstance(payload, Mapping):
            self._payload = payload


def _accessory_name(payload: Mapping[str, Any], serial: str) -> str:
    return str(_first_flat(payload, "name", "probeName", "accessoryName", "displayName") or f"KitchenAid Probe {serial}")


def _first_flat(payload: Mapping[str, Any], *keys: str) -> Any | None:
    flat = payload if all(not isinstance(v, (dict, list)) for v in payload.values()) else flatten(payload)
    for key in keys:
        value = find_key(flat, (key,))
        if value not in (None, ""):
            return value
    return None


def _normalize_number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _normalize_temperature(value: Any) -> float | None:
    numeric = _normalize_number(value)
    if numeric is None:
        return None
    return float(numeric) / 10 if abs(float(numeric)) > 250 else float(numeric)
