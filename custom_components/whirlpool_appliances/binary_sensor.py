"""Binary sensors for Whirlpool Appliances integration."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import (
    WhirlpoolApkEntity,
    attr_value,
    entity_name_from_key,
    find_key,
    has_legacy_attr,
    is_aircon_appliance,
    is_cooking_appliance,
    is_dishwasher_appliance,
    is_laundry_appliance,
    is_refrigeration_appliance,
    microwave_exists,
    oven_cavity_exists,
)


@dataclass(frozen=True, kw_only=True)
class WhirlpoolApkBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Mapping[str, Any]], bool | None]
    cooking_only: bool = False
    microwave_only: bool = False
    laundry_only: bool = False
    refrigeration_only: bool = False
    aircon_only: bool = False
    dishwasher_only: bool = False


def _bool(raw: Any, *, truthy_extra: tuple[str, ...] = ()) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.lower() in ("1", "true", "on", "open", "opened", "yes", "online", "connected", "running", "locked", *truthy_extra)
    return bool(raw)


def _bool_by_keys(*keys: str, invert: bool = False) -> Callable[[Mapping[str, Any]], bool | None]:
    def value(flat: Mapping[str, Any]) -> bool | None:
        val = _bool(find_key(flat, keys))
        return None if val is None else (not val if invert else val)
    return value


def _bool_attr(attr: str) -> Callable[[Mapping[str, Any]], bool | None]:
    return lambda flat: _bool(attr_value(flat, attr))


def _running(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("running", "isRunning", "cycleRunning"))
    if raw is not None:
        return _bool(raw)
    state = find_key(flat, ("applianceState", "machineState", "Cavity_CycleStatusMachineState"))
    if state is None:
        state = attr_value(flat, "OvenUpperCavity_OpStatusState") or attr_value(flat, "OvenLowerCavity_OpStatusState") or attr_value(flat, "Mwo_OperationStatusState")
    if state is None:
        return None
    return str(state).lower() in {"running", "preheating", "cooking", "7", "1", "2"}


def _door(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("doorOpen", "isDoorOpen", "doorStatus", "door"))
    if raw is not None:
        return _bool(raw)
    upper = _bool(attr_value(flat, "OvenUpperCavity_OpStatusDoorOpen"))
    lower = _bool(attr_value(flat, "OvenLowerCavity_OpStatusDoorOpen"))
    if upper is not None or lower is not None:
        return bool(upper) or bool(lower)
    return None


def _problem(flat: Mapping[str, Any]) -> bool:
    """Return true only when Whirlpool reports an actual problem/fault."""
    raw = find_key(
        flat,
        (
            "activeFault",
            "faultCode",
            "errorCode",
            "alarmCode",
            "Sys_AlertStatusCustomerFaultCode",
            "Sys_AlertStatusCustomerFaultCodeNotification",
            "error",
            "fault",
            "alarm",
            "hasError",
        ),
    )
    if raw in (None, "", 0, False):
        return False
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        return normalized not in {
            "",
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
        }
    return bool(raw)


def _is_laundry_status(flat: Mapping[str, Any]) -> bool:
    """Return true when status shape looks like a washer/dryer payload."""
    return any(
        find_key(flat, (key,)) is not None
        for key in (
            "washer.applianceState",
            "dryer.applianceState",
            "applianceState",
            "machineState",
            "washer.cycleName",
            "dryer.cycleName",
            "doorLockStatus",
            "washer.doorStatus",
            "dryer.doorStatus",
            "cleanWasher",
            "remoteStartEnable",
            "hmiControlLockout",
        )
    )


def _door_locked(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("washer.doorLockStatus", "dryer.doorLockStatus", "doorLockStatus"))
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "on", "yes", "locked", "lock"}
    return bool(raw)


def _remote_start_enabled(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("remoteStartEnable", "remoteStartEnabled", "remoteEnabled"))
    return _bool(raw)


def _clean_washer(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("washer.cleanWasher", "cleanWasher"))
    return _bool(raw)


def _control_lock_enabled(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("hmiControlLockout", "controlLock", "controlLockout"))
    return _bool(raw)


def _door_open_from_keys(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("doorStatus", "door.status", "refrigeratorDoorStatus", "freezerDoorStatus", "dishwasher.doorStatus"))
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "on", "yes", "open", "opened"}
    return bool(raw)


def _filter_problem(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("filterStatus", "waterFilterStatus", "airFilterStatus", "acFilterStatus"))
    if raw in (None, ""):
        return None
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "ok", "good", "normal", "clean", "clear", "none", "false"}
    return bool(raw)


def _vacation_mode(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("vacationMode", "VacationMode", "refrigerator.vacationMode"))
    return _bool(raw)


def _ice_maker_on(flat: Mapping[str, Any]) -> bool | None:
    raw = find_key(flat, ("iceMaker", "iceMakerEnabled", "iceMakerStatus"))
    return _bool(raw)


BINARY_DESCRIPTIONS: tuple[WhirlpoolApkBinarySensorDescription, ...] = (
    WhirlpoolApkBinarySensorDescription(key="online", translation_key="online", device_class=BinarySensorDeviceClass.CONNECTIVITY, value_fn=_bool_by_keys("Online", "online", "isOnline", "connected")),
    WhirlpoolApkBinarySensorDescription(key="door", translation_key="door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_door),
    WhirlpoolApkBinarySensorDescription(
        key="remote_control",
        translation_key="remote_control",
        icon="mdi:remote",
        value_fn=lambda flat: (
            _bool(attr_value(flat, "XCat_RemoteSetRemoteControlEnable"))
            if attr_value(flat, "XCat_RemoteSetRemoteControlEnable") is not None
            else _bool_by_keys("remoteControl", "remoteEnabled", "remoteStartEnabled", "remoteStartEnable")(flat)
        ),
    ),
    WhirlpoolApkBinarySensorDescription(key="running", translation_key="running", device_class=BinarySensorDeviceClass.RUNNING, value_fn=_running),
    WhirlpoolApkBinarySensorDescription(key="error", translation_key="error", icon="mdi:alert", device_class=BinarySensorDeviceClass.PROBLEM, value_fn=_problem),
    WhirlpoolApkBinarySensorDescription(key="laundry_door_locked", translation_key="laundry_door_locked", icon="mdi:lock", value_fn=_door_locked, laundry_only=True),
    WhirlpoolApkBinarySensorDescription(key="laundry_remote_start", translation_key="laundry_remote_start", icon="mdi:remote", value_fn=_remote_start_enabled, laundry_only=True),
    WhirlpoolApkBinarySensorDescription(key="laundry_clean_washer", translation_key="laundry_clean_washer", icon="mdi:washing-machine-alert", value_fn=_clean_washer, laundry_only=True),
    WhirlpoolApkBinarySensorDescription(key="laundry_control_lock", translation_key="laundry_control_lock", icon="mdi:lock", value_fn=_control_lock_enabled, laundry_only=True),
    WhirlpoolApkBinarySensorDescription(key="dishwasher_door", translation_key="dishwasher_door", icon="mdi:dishwasher", device_class=BinarySensorDeviceClass.DOOR, value_fn=_door_open_from_keys, dishwasher_only=True),
    WhirlpoolApkBinarySensorDescription(key="refrigerator_door", translation_key="refrigerator_door", icon="mdi:fridge", device_class=BinarySensorDeviceClass.DOOR, value_fn=_door_open_from_keys, refrigeration_only=True),
    WhirlpoolApkBinarySensorDescription(key="refrigerator_filter_problem", translation_key="refrigerator_filter_problem", icon="mdi:air-filter", device_class=BinarySensorDeviceClass.PROBLEM, value_fn=_filter_problem, refrigeration_only=True),
    WhirlpoolApkBinarySensorDescription(key="refrigerator_vacation_mode", translation_key="refrigerator_vacation_mode", icon="mdi:palm-tree", value_fn=_vacation_mode, refrigeration_only=True),
    WhirlpoolApkBinarySensorDescription(key="ice_maker", translation_key="ice_maker", icon="mdi:snowflake", value_fn=_ice_maker_on, refrigeration_only=True),
    WhirlpoolApkBinarySensorDescription(key="ac_filter_problem", translation_key="ac_filter_problem", icon="mdi:air-filter", device_class=BinarySensorDeviceClass.PROBLEM, value_fn=_filter_problem, aircon_only=True),
    WhirlpoolApkBinarySensorDescription(key="upper_door", translation_key="upper_door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_bool_attr("OvenUpperCavity_OpStatusDoorOpen"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="lower_door", translation_key="lower_door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_bool_attr("OvenLowerCavity_OpStatusDoorOpen"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="upper_door_locked", translation_key="upper_door_locked", icon="mdi:lock", device_class=BinarySensorDeviceClass.LOCK, value_fn=_bool_by_keys("OvenUpperCavity_OpStatusDoorLocked", invert=True), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="lower_door_locked", translation_key="lower_door_locked", icon="mdi:lock", device_class=BinarySensorDeviceClass.LOCK, value_fn=_bool_by_keys("OvenLowerCavity_OpStatusDoorLocked", invert=True), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(
        key="control_lock",
        translation_key="control_lock",
        device_class=BinarySensorDeviceClass.LOCK,
        value_fn=_bool_by_keys("Sys_OperationSetControlLock", invert=True),
        cooking_only=True,
    ),
    WhirlpoolApkBinarySensorDescription(key="sabbath_mode", translation_key="sabbath_mode", value_fn=_bool_attr("Sys_OperationSetSabbathModeEnabled"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="upper_meat_probe", translation_key="upper_meat_probe", value_fn=_bool_attr("OvenUpperCavity_AlertStatusMeatProbePluggedIn"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="lower_meat_probe", translation_key="lower_meat_probe", value_fn=_bool_attr("OvenLowerCavity_AlertStatusMeatProbePluggedIn"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="microwave_door", translation_key="microwave_door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_bool_attr("Mwo_OperationStatusDoorOpen"), cooking_only=True, microwave_only=True),
    WhirlpoolApkBinarySensorDescription(key="microwave_running", translation_key="microwave_running", icon="mdi:power-off", device_class=BinarySensorDeviceClass.RUNNING, value_fn=lambda flat: (None if (s := attr_value(flat, "Mwo_OperationStatusState")) is None else str(s) in {"1", "2", "3", "6", "8", "9", "10", "running", "cooking"}), cooking_only=True, microwave_only=True),
    WhirlpoolApkBinarySensorDescription(key="microwave_light", translation_key="microwave_light", value_fn=_bool_attr("Mwo_DisplaySetLightOn"), cooking_only=True, microwave_only=True, entity_registry_enabled_default=False),
    WhirlpoolApkBinarySensorDescription(key="microwave_turntable", translation_key="microwave_turntable", value_fn=_bool_attr("Mwo_CycleSetTurntable"), cooking_only=True, microwave_only=True),
)


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
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
        for desc in BINARY_DESCRIPTIONS:
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
            if cooking and desc.key in {
                "door",
                "running",
                "control_lock",
                "sabbath_mode",
                "microwave_light",
                "microwave_turntable",
            }:
                continue
            if desc.microwave_only and not has_mwo:
                continue
            if desc.key.startswith("upper_") and not oven_cavity_exists(flat, "upper"):
                continue
            if desc.key.startswith("lower_") and not oven_cavity_exists(flat, "lower"):
                continue
            if desc.key == "upper_meat_probe" and not has_legacy_attr(flat, "OvenUpperCavity_AlertStatusMeatProbePluggedIn"):
                continue
            if desc.key == "lower_meat_probe" and not has_legacy_attr(flat, "OvenLowerCavity_AlertStatusMeatProbePluggedIn"):
                continue
            entities.append(WhirlpoolApkBinarySensor(coordinator, appliance, desc))
    async_add_entities(entities)


class WhirlpoolApkBinarySensor(WhirlpoolApkEntity, BinarySensorEntity):
    entity_description: WhirlpoolApkBinarySensorDescription

    def __init__(self, coordinator, appliance: Mapping[str, Any], description: WhirlpoolApkBinarySensorDescription) -> None:
        super().__init__(coordinator, appliance, description.key)
        self.entity_description = description
        self._attr_name = entity_name_from_key(description.translation_key or description.key, appliance)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.flat_status)

    @property
    def icon(self) -> str | None:
        if self.entity_description.key == "microwave_running":
            return "mdi:power-on" if self.is_on else "mdi:power-off"
        if self.entity_description.key.endswith("_door_locked"):
            return "mdi:lock" if self.is_on else "mdi:lock-open-variant"
        return self.entity_description.icon
