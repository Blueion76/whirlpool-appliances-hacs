"""Sensors for Whirlpool Appliances integration."""
from __future__ import annotations

import json
import time
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


def _attr_update_time(flat: Mapping[str, Any], attr: str) -> int | None:
    return _int_value(
        find_key(
            flat,
            (
                f"attributes.{attr}.updateTime",
                f"{attr}.updateTime",
                f"{attr}_updateTime",
            ),
        )
    )


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

    # Minerva ovens often leave TimeStatusCycleTimeElapsed at the factory
    # default 0. While actively preheating/cooking, derive elapsed time from the
    # most recent operation/state timestamp.
    start_ms = (
        _attr_update_time(flat, f"{prefix}_OpSetOperations")
        or _attr_update_time(flat, f"{prefix}_OpStatusState")
    )
    if not start_ms:
        return None

    elapsed = int((time.time() * 1000 - start_ms) / 1000)
    if elapsed < 0 or elapsed > 14 * 24 * 60 * 60:
        return None
    return elapsed


def _oven_cook_mode(flat: Mapping[str, Any], cavity: str) -> Any | None:
    prefix = _oven_prefix(cavity)
    raw = attr_value(flat, f"{prefix}_CycleSetCommonMode")
    state = str(attr_value(flat, f"{prefix}_OpStatusState") or "")
    if raw in (None, "0", 0) and state in {"1", "2"}:
        # Some combo models keep CycleSetCommonMode at 0 after a remote start.
        # Do not infer from target temperature because high-temp bake can look
        # like broil. The climate entity can remember the last HA-commanded
        # preset during runtime; this diagnostic sensor should stay unknown
        # unless Whirlpool reports a real mode.
        return None
    return COOK_MODE.get(str(raw), raw) if raw is not None else None


def _is_factory_default_attr(flat: Mapping[str, Any], attr: str) -> bool:
    """Return true for unchanged Whirlpool placeholder defaults.

    The Minerva microwave payload includes many zero-valued microwave fields
    whose updateTime is the same 2018 factory/default timestamp. Treating those
    as live values makes HA show cook power/time values permanently as 0.
    """
    value = attr_value(flat, attr)
    updated = _attr_update_time(flat, attr)
    return str(value or "") == "0" and updated is not None and updated <= 1540000000000


def _mwo_cook_time_set_seconds(flat: Mapping[str, Any]) -> int | None:
    value = _int_value(attr_value(flat, "Mwo_TimeSetCookTimeSet"))
    if value is None or value <= 0:
        return None
    return value


def _mwo_is_active(flat: Mapping[str, Any]) -> bool:
    state = str(attr_value(flat, "Mwo_OperationStatusState") or "")
    if state in {"2", "3"}:  # running or paused
        return True

    operation = str(attr_value(flat, "Mwo_OperationSetOperations") or "")
    start_ms = _attr_update_time(flat, "Mwo_OperationSetOperations")
    set_seconds = _mwo_cook_time_set_seconds(flat)
    if operation == "2" and start_ms and set_seconds:
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
    if operation != "2" or not start_ms or not set_seconds:
        return None

    elapsed = int((time.time() * 1000 - start_ms) / 1000)
    if elapsed < 0 or elapsed > set_seconds + 300:
        return None
    return min(elapsed, set_seconds)


def _mwo_remaining_seconds(flat: Mapping[str, Any]) -> int | None:
    raw = _int_value(attr_value(flat, "Mwo_TimeStatusCookTimeRemaining"))
    if raw and raw > 0:
        return raw

    set_seconds = _mwo_cook_time_set_seconds(flat)
    elapsed = _mwo_elapsed_seconds(flat)
    if set_seconds is None or elapsed is None:
        return None
    return max(set_seconds - elapsed, 0)


def _mwo_cook_power(flat: Mapping[str, Any]) -> int | None:
    raw = _int_value(attr_value(flat, "Mwo_CycleSetCookPower"))
    if raw and raw > 0:
        return raw

    # A zero with the factory/default updateTime is not a real "0% power"
    # reading. Standard microwave cook defaults to full power when no explicit
    # power level is reported.
    if _is_factory_default_attr(flat, "Mwo_CycleSetCookPower"):
        return 100 if _mwo_is_active(flat) else None

    return raw


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
    if value in (None, "", 0, False):
        return "Clear"
    if isinstance(value, str) and value.strip().lower() in {
        "0",
        "false",
        "none",
        "no",
        "clear",
        "ok",
        "normal",
        "no_fault",
        "no fault",
        "no_error",
        "no error",
    }:
        return "Clear"
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
    upper = _oven_cook_mode(flat, "upper")
    if upper not in (None, "Standby"):
        return upper
    lower = _oven_cook_mode(flat, "lower")
    if lower is not None:
        return lower
    return upper


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
    WhirlpoolApkSensorDescription(key="fault_code", translation_key="fault_code", icon="mdi:alert", value_fn=_active_fault),
    WhirlpoolApkSensorDescription(key="cook_mode", translation_key="cook_mode", icon="mdi:chef-hat", device_class=SensorDeviceClass.ENUM, options=list(COOK_MODE.values()), value_fn=_generic_cook_mode, cooking_only=True),
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
    def icon(self) -> str | None:
        if self.entity_description.key == "microwave_state":
            return "mdi:microwave" if self.native_value in {"Setting", "Running", "Paused"} else "mdi:microwave-off"
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
