"""Number entities for Whirlpool setpoints."""
from __future__ import annotations

import asyncio
import logging

from collections.abc import Mapping
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import UnitOfTime, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, celsius_to_unit, entity_name_from_key, find_key, is_cooking_appliance, oven_cavity_exists, unit_to_celsius
from .oven_options import current_oven_options, local_options, minutes_to_seconds, oven_is_active, update_local_options
from .logging_utils import summarize

_LOGGER = logging.getLogger(__name__)


def _temp_from_tenths(value) -> float | None:
    if value in (None, "", "0", 0):
        return None
    try:
        return int(value) / 10
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _fahrenheit_to_celsius(value: float) -> float:
    return (float(value) - 32) * 5 / 9


def _allowed_oven_temperatures(unit: UnitOfTemperature) -> tuple[float, ...]:
    temps_f = tuple(range(175, 551, 5))
    if unit == UnitOfTemperature.FAHRENHEIT:
        return tuple(float(v) for v in temps_f)
    return tuple(round(_fahrenheit_to_celsius(v), 1) for v in temps_f)


def _snap_oven_temperature(value: float, unit: UnitOfTemperature) -> float:
    allowed = _allowed_oven_temperatures(unit)
    return min(allowed, key=lambda allowed_value: abs(allowed_value - float(value)))


_OVEN_MODE_CODE_TO_SERVICE_MODE = {
    "2": "bake",
    "6": "convect_bake",
    "8": "broil",
    "9": "convect_broil",
    "16": "convect_roast",
    "24": "keep_warm",
    "41": "air_fry",
}
_OVEN_COMPLETE_ACTION_CODE_TO_SERVICE = {
    "1": "stay_on",
    "2": "keep_warm",
    "3": "turn_off",
}


def _cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def _timer_seconds(flat: Mapping[str, object]) -> int | None:
    raw = attr_value(flat, "KitchenTimer01_SetTimeSet")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _seconds_value(flat: Mapping[str, object], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _seconds_to_minutes(value: int | None) -> float | None:
    if value is None:
        return None
    minutes = value / 60
    return int(minutes) if minutes.is_integer() else round(minutes, 1)


def _minutes_to_seconds(value: float) -> int:
    return max(0, int(round(float(value) * 60)))


def _oven_is_active(flat: Mapping[str, object], cavity: str | None) -> bool:
    return str(attr_value(flat, f"{_cavity_prefix(cavity)}_OpStatusState") or "") in {"1", "2"}


def _current_oven_options(flat: Mapping[str, object], cavity: str | None) -> dict[str, object]:
    """Build the full oven command options needed to restart/apply a change."""
    prefix = _cavity_prefix(cavity)

    mode_code = str(attr_value(flat, f"{prefix}_CycleSetCommonMode") or "")
    mode = _OVEN_MODE_CODE_TO_SERVICE_MODE.get(mode_code, "bake")

    target_temp = _temp_from_tenths(attr_value(flat, f"{prefix}_CycleSetTargetTemp")) or 176.6
    cook_time = _seconds_value(flat, f"{prefix}_TimeSetCookTimeSet")
    delay_time = _seconds_value(flat, f"{prefix}_TimeSetDelayTime")

    action_code = str(attr_value(flat, f"{prefix}_OpSetCookTimeCompleteAction") or "3")
    complete_action = _OVEN_COMPLETE_ACTION_CODE_TO_SERVICE.get(action_code, "turn_off")

    return {
        "mode": mode,
        "target_temp": target_temp,
        "cook_time_seconds": cook_time,
        "delay_time_seconds": delay_time,
        "complete_action": complete_action,
    }


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            if oven_cavity_exists(flat, "upper"):
                entities.append(WhirlpoolTargetTemperatureNumber(coordinator, appliance, "upper"))
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "upper", "cook_time"))
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "upper", "delay_time"))
            if oven_cavity_exists(flat, "lower"):
                entities.append(WhirlpoolTargetTemperatureNumber(coordinator, appliance, "lower"))
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "lower", "cook_time"))
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "lower", "delay_time"))
            entities.append(WhirlpoolKitchenTimerNumber(coordinator, appliance))
            continue
        # Phase 7 safety guard: do not expose generic writable setpoints for
        # appliances we cannot test. Read-only temperature sensors are still
        # created by sensor.py; writable controls should be added per category
        # only after DDM/captures confirm the command payload.
        continue
    async_add_entities(entities)


class WhirlpoolTargetTemperatureNumber(WhirlpoolApkEntity, NumberEntity):
    """Local oven target temperature option.

    When idle, this stores the desired temperature locally for the Start Oven
    button. When cooking, it cancels and restarts the current cycle so the new
    temperature applies in the full command payload.
    """

    def __init__(self, coordinator, appliance: Mapping[str, object], cavity: str | None) -> None:
        self.cavity = cavity
        suffix = f"{cavity}_target_temperature_setpoint" if cavity else "target_temperature_setpoint"
        super().__init__(coordinator, appliance, suffix)
        unit = self.temperature_unit
        self.entity_description = NumberEntityDescription(
            key=suffix,
            translation_key=suffix,
            native_min_value=_allowed_oven_temperatures(unit)[0],
            native_max_value=_allowed_oven_temperatures(unit)[-1],
            native_step=5,
            native_unit_of_measurement=unit,
            mode=NumberMode.BOX,
        )
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> float | None:
        local = local_options(self.coordinator, self.said, self.cavity)
        if "target_temp" in local:
            return _snap_oven_temperature(celsius_to_unit(local["target_temp"], self.temperature_unit), self.temperature_unit)

        if self.cavity == "upper":
            value = _temp_from_tenths(attr_value(self.flat_status, "OvenUpperCavity_CycleSetTargetTemp"))
        elif self.cavity == "lower":
            value = _temp_from_tenths(attr_value(self.flat_status, "OvenLowerCavity_CycleSetTargetTemp"))
        else:
            raw = find_key(self.flat_status, ("targetTemperature", "targetTemp", "setTemperature"))
            try:
                value = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                value = None
        if value is None and self.cavity in ("upper", "lower"):
            # Idle/default oven setpoint: 175°F = 79.4°C.
            value = 79.4
        if value is None:
            return None
        display_value = celsius_to_unit(value, self.temperature_unit)
        if self.cavity in ("upper", "lower") and display_value is not None:
            return _snap_oven_temperature(display_value, self.temperature_unit)
        return display_value

    async def async_set_native_value(self, value: float) -> None:
        snapped = _snap_oven_temperature(value, self.temperature_unit)
        celsius = unit_to_celsius(snapped, self.temperature_unit) or 176.6

        if self.cavity in ("upper", "lower") and oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["target_temp"] = celsius
            _LOGGER.debug("Changing active Whirlpool oven target temperature: entity=%s said=%s cavity=%s value=%s%s target_c=%s options=%s", self.entity_id, self.said, self.cavity, value, self.temperature_unit, celsius, summarize(options))
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

        # Idle: do not send a partial attribute. Store it for the Start Oven button.
        _LOGGER.debug("Stored pending Whirlpool oven target temperature: entity=%s said=%s cavity=%s value=%s%s target_c=%s", self.entity_id, self.said, self.cavity, value, self.temperature_unit, celsius)
        update_local_options(self.coordinator, self.said, self.cavity, target_temp=celsius)
        self.async_write_ha_state()



class WhirlpoolOvenTimeNumber(WhirlpoolApkEntity, NumberEntity):
    """Oven cook-time and delay-time number entities."""

    def __init__(self, coordinator, appliance: Mapping[str, object], cavity: str, kind: str) -> None:
        self.cavity = cavity
        self.kind = kind
        suffix = f"{cavity}_oven_{kind}"
        super().__init__(coordinator, appliance, suffix)
        self.entity_description = NumberEntityDescription(
            key=suffix,
            translation_key=suffix,
            icon="mdi:timer" if kind == "cook_time" else "mdi:timer-outline",
            native_min_value=0,
            native_max_value=719,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            mode=NumberMode.BOX,
        )
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> int | None:
        local = local_options(self.coordinator, self.said, self.cavity)
        local_key = "cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes"
        if local_key in local:
            return float(local[local_key])
        prefix = _cavity_prefix(self.cavity)
        attr = f"{prefix}_TimeSetCookTimeSet" if self.kind == "cook_time" else f"{prefix}_TimeSetDelayTime"
        return _seconds_to_minutes(_seconds_value(self.flat_status, attr))

    async def async_set_native_value(self, value: float) -> None:
        prefix = _cavity_prefix(self.cavity)
        minutes = max(0, float(value))
        attr = f"{prefix}_TimeSetCookTimeSet" if self.kind == "cook_time" else f"{prefix}_TimeSetDelayTime"

        # Whirlpool applies cook-time/delay-time as part of the complete oven
        # cycle command. If active, cancel/restart with the changed timing value.
        # If idle, store locally for the Start Oven button instead of sending a
        # partial attribute the appliance will not keep.
        if oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            if self.kind == "cook_time":
                options["cook_time_minutes"] = minutes
            else:
                options["delay_time_minutes"] = minutes
            _LOGGER.debug("Changing active Whirlpool oven %s: entity=%s said=%s cavity=%s minutes=%s options=%s", self.kind, self.entity_id, self.said, self.cavity, minutes, summarize(options))

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

        if self.kind == "cook_time":
            _LOGGER.debug("Stored pending Whirlpool oven cook time: entity=%s said=%s cavity=%s minutes=%s", self.entity_id, self.said, self.cavity, minutes)
            update_local_options(self.coordinator, self.said, self.cavity, cook_time_minutes=minutes)
        else:
            _LOGGER.debug("Stored pending Whirlpool oven delay time: entity=%s said=%s cavity=%s minutes=%s", self.entity_id, self.said, self.cavity, minutes)
            update_local_options(self.coordinator, self.said, self.cavity, delay_time_minutes=minutes)
        self.async_write_ha_state()



class WhirlpoolKitchenTimerNumber(WhirlpoolApkEntity, NumberEntity):
    """Typed seconds input for the Whirlpool on-screen kitchen timer."""

    def __init__(self, coordinator, appliance: Mapping[str, object]) -> None:
        super().__init__(coordinator, appliance, "kitchen_timer_1_set")
        self.entity_description = NumberEntityDescription(
            key="kitchen_timer_1_set",
            translation_key="kitchen_timer_1_set",
            icon="mdi:timer",
            native_min_value=0,
            native_max_value=23 * 60 * 60 + 59 * 60 + 59,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            mode=NumberMode.BOX,
        )
        self._attr_name = entity_name_from_key("kitchen_timer_1_set", appliance)

    @property
    def native_value(self) -> int | None:
        return _timer_seconds(self.flat_status)

    async def async_set_native_value(self, value: float) -> None:
        self._check_service_request(await self.client.set_kitchen_timer(self.said, int(value), 1))
        await self.coordinator.async_request_refresh()
