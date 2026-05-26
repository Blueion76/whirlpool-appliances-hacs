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
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, find_key, has_legacy_attr, is_cooking_appliance, microwave_exists, oven_cavity_exists


@dataclass(frozen=True, kw_only=True)
class WhirlpoolApkBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[Mapping[str, Any]], bool | None]
    cooking_only: bool = False
    microwave_only: bool = False


def _bool(raw: Any, *, truthy_extra: tuple[str, ...] = ()) -> bool | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw.lower() in ("1", "true", "on", "open", "opened", "yes", "online", "connected", "running", *truthy_extra)
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
    """Return true only when Whirlpool reports an actual problem/fault.

    Home Assistant's ``problem`` binary sensor device class displays
    ``off`` as Clear and ``on`` as Problem. Missing/empty/no-fault values should
    therefore be False instead of None, otherwise the entity shows Unknown even
    when there is no issue.
    """
    raw = find_key(
        flat,
        (
            "activeFault",
            "faultCode",
            "errorCode",
            "alarmCode",
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


BINARY_DESCRIPTIONS: tuple[WhirlpoolApkBinarySensorDescription, ...] = (
    WhirlpoolApkBinarySensorDescription(key="online", translation_key="online", device_class=BinarySensorDeviceClass.CONNECTIVITY, value_fn=_bool_by_keys("Online", "online", "isOnline", "connected")),
    WhirlpoolApkBinarySensorDescription(key="door", translation_key="door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_door),
    WhirlpoolApkBinarySensorDescription(
        key="remote_control",
        translation_key="remote_control",
        value_fn=lambda flat: (
            _bool(attr_value(flat, "XCat_RemoteSetRemoteControlEnable"))
            if attr_value(flat, "XCat_RemoteSetRemoteControlEnable") is not None
            else _bool_by_keys("remoteControl", "remoteEnabled", "remoteStartEnabled", "remoteStartEnable")(flat)
        ),
    ),
    WhirlpoolApkBinarySensorDescription(key="running", translation_key="running", device_class=BinarySensorDeviceClass.RUNNING, value_fn=_running),
    WhirlpoolApkBinarySensorDescription(key="error", translation_key="error", device_class=BinarySensorDeviceClass.PROBLEM, value_fn=_problem),
    WhirlpoolApkBinarySensorDescription(key="upper_door", translation_key="upper_door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_bool_attr("OvenUpperCavity_OpStatusDoorOpen"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="lower_door", translation_key="lower_door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_bool_attr("OvenLowerCavity_OpStatusDoorOpen"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(
        key="control_lock",
        translation_key="control_lock",
        device_class=BinarySensorDeviceClass.LOCK,
        # Whirlpool uses 1 = control lock enabled/locked and 0 = unlocked.
        # Home Assistant binary_sensor lock device_class uses on = unlocked,
        # off = locked, so invert the appliance attribute for the binary sensor.
        value_fn=_bool_by_keys("Sys_OperationSetControlLock", invert=True),
        cooking_only=True,
    ),
    WhirlpoolApkBinarySensorDescription(key="sabbath_mode", translation_key="sabbath_mode", value_fn=_bool_attr("Sys_OperationSetSabbathModeEnabled"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="upper_meat_probe", translation_key="upper_meat_probe", value_fn=_bool_attr("OvenUpperCavity_AlertStatusMeatProbePluggedIn"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="lower_meat_probe", translation_key="lower_meat_probe", value_fn=_bool_attr("OvenLowerCavity_AlertStatusMeatProbePluggedIn"), cooking_only=True),
    WhirlpoolApkBinarySensorDescription(key="microwave_door", translation_key="microwave_door", device_class=BinarySensorDeviceClass.DOOR, value_fn=_bool_attr("Mwo_OperationStatusDoorOpen"), cooking_only=True, microwave_only=True),
    WhirlpoolApkBinarySensorDescription(key="microwave_running", translation_key="microwave_running", device_class=BinarySensorDeviceClass.RUNNING, value_fn=lambda flat: (None if (s := attr_value(flat, "Mwo_OperationStatusState")) is None else str(s) in {"1", "2", "running", "cooking"}), cooking_only=True, microwave_only=True),
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
        for desc in BINARY_DESCRIPTIONS:
            if desc.cooking_only and not cooking:
                continue
            if cooking and desc.key in {
                "door",
                "running",
                "control_lock",
                "sabbath_mode",
                "microwave_light",
                "microwave_turntable",
            }:
                # These duplicate more useful entities on combo cooking models:
                # oven/microwave door sensors, oven/microwave state sensors,
                # the control-lock switch, the Sabbath switch, the microwave
                # light entity, and the microwave turntable switch.
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



