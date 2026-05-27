"""Number entities for Whirlpool setpoints."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .const import DOMAIN
from .control_helpers import frozen_or_custom_cycle, microwave_local_options, oven_cook_attrs, oven_is_active, raise_if_common_blocked, update_microwave_options
from .entity import WhirlpoolApkEntity, attr_value, celsius_to_unit, entity_name_from_key, find_key, is_cooking_appliance, microwave_exists, oven_cavity_exists, unit_to_celsius
from .logging_utils import summarize
from .oven_options import current_oven_options, local_options, minutes_to_seconds, update_local_options

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


def _cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def _timer_seconds(flat: Mapping[str, object]) -> int | None:
    try:
        value = int(attr_value(flat, "KitchenTimer01_SetTimeSet"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _seconds_value(flat: Mapping[str, object], attr: str) -> int | None:
    try:
        value = int(attr_value(flat, attr))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _seconds_to_minutes(value: int | None) -> float | None:
    if value is None:
        return None
    minutes = value / 60
    return int(minutes) if minutes.is_integer() else round(minutes, 1)


def _int_attr(flat: Mapping[str, Any], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


async def _send_oven_options(entity: WhirlpoolApkEntity, cavity: str, options: Mapping[str, Any], *, delay_time_change: bool = False) -> None:
    active = oven_is_active(entity.flat_status, cavity)
    raise_if_common_blocked(entity.flat_status, cavity=cavity)
    if active and frozen_or_custom_cycle(entity.flat_status, cavity):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="modify_not_allowed")
    if active and delay_time_change:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="delay_change_not_allowed")
    attrs = oven_cook_attrs(
        cavity=cavity,
        temperature=float(options["target_temp"]),
        mode=str(options["mode"]),
        cook_time_seconds=minutes_to_seconds(options.get("cook_time_minutes")),
        delay_time_seconds=minutes_to_seconds(options.get("delay_time_minutes")),
        complete_action=str(options["complete_action"]),
        operation="4" if active else "2",
    )
    _LOGGER.debug("Applying Whirlpool oven attrs from number entity: entity=%s said=%s cavity=%s attrs=%s", entity.entity_id, entity.said, cavity, summarize(attrs))
    entity._check_service_request(await entity.client.send_attributes(entity.said, attrs))
    await entity.coordinator.async_request_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            if oven_cavity_exists(flat, "upper"):
                entities += [
                    WhirlpoolTargetTemperatureNumber(coordinator, appliance, "upper"),
                    WhirlpoolOvenTimeNumber(coordinator, appliance, "upper", "cook_time"),
                    WhirlpoolOvenTimeNumber(coordinator, appliance, "upper", "delay_time"),
                ]
            if oven_cavity_exists(flat, "lower"):
                entities += [
                    WhirlpoolTargetTemperatureNumber(coordinator, appliance, "lower"),
                    WhirlpoolOvenTimeNumber(coordinator, appliance, "lower", "cook_time"),
                    WhirlpoolOvenTimeNumber(coordinator, appliance, "lower", "delay_time"),
                ]
            entities.append(WhirlpoolKitchenTimerNumber(coordinator, appliance))
            entities.append(WhirlpoolDisplayBrightnessNumber(coordinator, appliance))
            if microwave_exists(flat):
                entities += [
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_cook_time", "cook_time_seconds"),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_cook_power", "cook_power"),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_amount", "amount"),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_target_temperature", "target_temperature"),
                ]
    async_add_entities(entities)


class WhirlpoolTargetTemperatureNumber(WhirlpoolApkEntity, NumberEntity):
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
            value = 79.4
        if value is None:
            return None
        display_value = celsius_to_unit(value, self.temperature_unit)
        return _snap_oven_temperature(display_value, self.temperature_unit) if display_value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        snapped = _snap_oven_temperature(value, self.temperature_unit)
        celsius = unit_to_celsius(snapped, self.temperature_unit) or 176.6
        if self.cavity in ("upper", "lower") and oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["target_temp"] = celsius
            await _send_oven_options(self, self.cavity, options)
            return
        update_local_options(self.coordinator, self.said, self.cavity, target_temp=celsius)
        self.async_write_ha_state()


class WhirlpoolOvenTimeNumber(WhirlpoolApkEntity, NumberEntity):
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
        minutes = max(0, float(value))
        if oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes"] = minutes
            await _send_oven_options(self, self.cavity, options, delay_time_change=self.kind == "delay_time")
            return
        update_local_options(self.coordinator, self.said, self.cavity, **{"cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes": minutes})
        self.async_write_ha_state()


class WhirlpoolKitchenTimerNumber(WhirlpoolApkEntity, NumberEntity):
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


class WhirlpoolDisplayBrightnessNumber(WhirlpoolApkEntity, NumberEntity):
    def __init__(self, coordinator, appliance: Mapping[str, object]) -> None:
        super().__init__(coordinator, appliance, "display_brightness_percent")
        self.entity_description = NumberEntityDescription(
            key="display_brightness_percent",
            translation_key="display_brightness_percent",
            icon="mdi:brightness-6",
            native_min_value=0,
            native_max_value=100,
            native_step=1,
            native_unit_of_measurement=PERCENTAGE,
            mode=NumberMode.SLIDER,
        )
        self._attr_name = entity_name_from_key("display_brightness_percent", appliance)

    @property
    def native_value(self) -> int | None:
        return _int_attr(self.flat_status, "Sys_DisplaySetBrightnessPercent")

    async def async_set_native_value(self, value: float) -> None:
        raise_if_common_blocked(self.flat_status)
        self._check_service_request(await self.client.send_attributes(self.said, {"Sys_DisplaySetBrightnessPercent": str(max(0, min(100, int(value))))}))
        await self.coordinator.async_request_refresh()


class WhirlpoolMicrowaveNumber(WhirlpoolApkEntity, NumberEntity):
    def __init__(self, coordinator, appliance: Mapping[str, object], key: str, option_key: str) -> None:
        self.option_key = option_key
        super().__init__(coordinator, appliance, key)
        if key == "microwave_cook_time":
            desc = NumberEntityDescription(key=key, translation_key=key, icon="mdi:timer", native_min_value=1, native_max_value=7200, native_step=1, native_unit_of_measurement=UnitOfTime.SECONDS, mode=NumberMode.BOX)
        elif key == "microwave_cook_power":
            desc = NumberEntityDescription(key=key, translation_key=key, icon="mdi:lightning-bolt", native_min_value=1, native_max_value=100, native_step=1, native_unit_of_measurement=PERCENTAGE, mode=NumberMode.SLIDER)
        elif key == "microwave_amount":
            desc = NumberEntityDescription(key=key, translation_key=key, icon="mdi:scale", native_min_value=0, native_max_value=65535, native_step=1, mode=NumberMode.BOX)
        else:
            desc = NumberEntityDescription(key=key, translation_key=key, icon="mdi:thermometer", native_min_value=0, native_max_value=300, native_step=1, native_unit_of_measurement=UnitOfTemperature.CELSIUS, mode=NumberMode.BOX)
        self.entity_description = desc
        self._attr_name = entity_name_from_key(key, appliance)

    @property
    def native_value(self) -> float | None:
        options = microwave_local_options(self.coordinator, self.said)
        if self.option_key in options and options[self.option_key] is not None:
            return float(options[self.option_key])
        attr = {
            "cook_time_seconds": "Mwo_TimeSetCookTimeSet",
            "cook_power": "Mwo_CycleSetCookPower",
            "amount": "Mwo_CycleSetAmount",
            "target_temperature": "Mwo_CycleSetTargetTemp",
        }[self.option_key]
        raw = _int_attr(self.flat_status, attr)
        if raw is None:
            return None
        return raw / 10 if self.option_key == "target_temperature" else raw

    async def async_set_native_value(self, value: float) -> None:
        update_microwave_options(self.coordinator, self.said, **{self.option_key: round(float(value), 1) if self.option_key == "target_temperature" else int(value)})
        self.async_write_ha_state()
