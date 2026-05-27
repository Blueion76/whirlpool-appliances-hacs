"""Number entities for Whirlpool setpoints, brightness, and duration options."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTemperature, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .const import DOMAIN
from .helpers.control import (
    frozen_or_custom_cycle,
    microwave_local_options,
    oven_cook_attrs,
    oven_is_active,
    raise_if_common_blocked,
    update_microwave_options,
)
from .entity import (
    WhirlpoolApkEntity,
    attr_value,
    entity_name_from_key,
    find_key,
    is_cooking_appliance,
    microwave_exists,
    oven_cavity_exists,
)
from .helpers.logging import summarize
from .helpers.oven_options import current_oven_options, local_options, minutes_to_seconds, update_local_options

_LOGGER = logging.getLogger(__name__)


def _temp_from_tenths(value: Any) -> float | None:
    """Convert Whirlpool tenths-of-Celsius values to Celsius."""
    if value in (None, "", "0", 0):
        return None
    try:
        return int(value) / 10
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _int_attr(flat: Mapping[str, Any], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def _seconds_value(flat: Mapping[str, Any], attr: str) -> int | None:
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


async def _send_oven_options(entity: WhirlpoolApkEntity, cavity: str, options: Mapping[str, Any], *, delay_time_change: bool = False) -> None:
    """Apply oven options when the oven is already running."""
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
    _LOGGER.debug(
        "Applying Whirlpool oven attrs from number entity: entity=%s said=%s cavity=%s attrs=%s",
        entity.entity_id,
        entity.said,
        cavity,
        summarize(attrs),
    )
    entity._check_service_request(await entity.client.send_attributes(entity.said, attrs))
    await entity.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WhirlpoolApkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Whirlpool number entities."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []

    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance) or not is_cooking_appliance(appliance):
            continue

        flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
        if oven_cavity_exists(flat, "upper"):
            entities.extend(
                (
                    WhirlpoolTargetTemperatureNumber(coordinator, appliance, "upper"),
                    WhirlpoolOvenDurationNumber(coordinator, appliance, "upper", "cook_time"),
                    WhirlpoolOvenDurationNumber(coordinator, appliance, "upper", "delay_time"),
                )
            )
        if oven_cavity_exists(flat, "lower"):
            entities.extend(
                (
                    WhirlpoolTargetTemperatureNumber(coordinator, appliance, "lower"),
                    WhirlpoolOvenDurationNumber(coordinator, appliance, "lower", "cook_time"),
                    WhirlpoolOvenDurationNumber(coordinator, appliance, "lower", "delay_time"),
                )
            )

        entities.append(WhirlpoolKitchenTimerNumber(coordinator, appliance))
        entities.append(WhirlpoolDisplayBrightnessNumber(coordinator, appliance))

        if microwave_exists(flat):
            entities.extend(
                (
                    WhirlpoolMicrowaveDurationNumber(coordinator, appliance),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_cook_power", "cook_power"),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_amount", "amount"),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_target_temperature", "target_temperature"),
                )
            )

    async_add_entities(entities)


class WhirlpoolTargetTemperatureNumber(WhirlpoolApkEntity, NumberEntity):
    """Pending oven target temperature selector.

    Native value is Celsius. Home Assistant can convert/display it in the
    user's preferred unit because this is a temperature number entity.
    """

    def __init__(self, coordinator, appliance: Mapping[str, object], cavity: str | None) -> None:
        self.cavity = cavity
        suffix = f"{cavity}_target_temperature_setpoint" if cavity else "target_temperature_setpoint"
        super().__init__(coordinator, appliance, suffix)
        self.entity_description = NumberEntityDescription(
            key=suffix,
            translation_key=suffix,
            device_class=NumberDeviceClass.TEMPERATURE,
            native_min_value=79,
            native_max_value=288,
            native_step=1,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            mode=NumberMode.BOX,
            suggested_display_precision=0,
        )
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> float | None:
        local = local_options(self.coordinator, self.said, self.cavity)
        if "target_temp" in local:
            return round(float(local["target_temp"]))

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
        return round(float(value)) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        celsius = round(float(value))
        if self.cavity in ("upper", "lower") and oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["target_temp"] = celsius
            await _send_oven_options(self, self.cavity, options)
            return
        update_local_options(self.coordinator, self.said, self.cavity, target_temp=celsius)
        self.async_write_ha_state()


class WhirlpoolOvenDurationNumber(WhirlpoolApkEntity, NumberEntity):
    """Oven cook/delay duration in minutes."""

    def __init__(self, coordinator, appliance: Mapping[str, object], cavity: str, kind: str) -> None:
        self.cavity = cavity
        self.kind = kind
        suffix = f"{cavity}_oven_{kind}"
        super().__init__(coordinator, appliance, suffix)
        self.entity_description = NumberEntityDescription(
            key=suffix,
            translation_key=suffix,
            icon="mdi:timer" if kind == "cook_time" else "mdi:timer-outline",
            device_class=NumberDeviceClass.DURATION,
            native_min_value=0,
            native_max_value=719,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            mode=NumberMode.BOX,
            suggested_display_precision=0,
        )
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> float | None:
        local = local_options(self.coordinator, self.said, self.cavity)
        local_key = "cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes"
        if local_key in local:
            return round(float(local[local_key]))
        prefix = _cavity_prefix(self.cavity)
        attr = f"{prefix}_TimeSetCookTimeSet" if self.kind == "cook_time" else f"{prefix}_TimeSetDelayTime"
        value = _seconds_to_minutes(_seconds_value(self.flat_status, attr))
        return round(float(value)) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        minutes = max(0, round(float(value)))
        if oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes"] = minutes
            await _send_oven_options(self, self.cavity, options, delay_time_change=self.kind == "delay_time")
            return
        update_local_options(
            self.coordinator,
            self.said,
            self.cavity,
            **{"cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes": minutes},
        )
        self.async_write_ha_state()


class WhirlpoolKitchenTimerNumber(WhirlpoolApkEntity, NumberEntity):
    """Kitchen timer duration in minutes."""

    def __init__(self, coordinator, appliance: Mapping[str, object]) -> None:
        super().__init__(coordinator, appliance, "kitchen_timer_1_set")
        self.entity_description = NumberEntityDescription(
            key="kitchen_timer_1_set",
            translation_key="kitchen_timer_1_set",
            icon="mdi:timer",
            device_class=NumberDeviceClass.DURATION,
            native_min_value=0,
            native_max_value=23 * 60 + 59,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            mode=NumberMode.BOX,
            suggested_display_precision=0,
        )
        self._attr_name = entity_name_from_key("kitchen_timer_1_set", appliance)

    @property
    def native_value(self) -> float | None:
        value = _seconds_to_minutes(_seconds_value(self.flat_status, "KitchenTimer01_SetTimeSet"))
        return round(float(value)) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        minutes = max(0, round(float(value)))
        self._check_service_request(await self.client.set_kitchen_timer(self.said, minutes * 60, 1))
        await self.coordinator.async_request_refresh()


class WhirlpoolDisplayBrightnessNumber(WhirlpoolApkEntity, NumberEntity):
    """Display brightness percentage control."""

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
            suggested_display_precision=0,
        )
        self._attr_name = entity_name_from_key("display_brightness_percent", appliance)

    @property
    def native_value(self) -> int | None:
        return _int_attr(self.flat_status, "Sys_DisplaySetBrightnessPercent")

    async def async_set_native_value(self, value: float) -> None:
        raise_if_common_blocked(self.flat_status)
        self._check_service_request(
            await self.client.send_attributes(
                self.said,
                {"Sys_DisplaySetBrightnessPercent": str(max(0, min(100, int(value))))},
            )
        )
        await self.coordinator.async_request_refresh()


class WhirlpoolMicrowaveDurationNumber(WhirlpoolApkEntity, NumberEntity):
    """Microwave cook duration in minutes."""

    def __init__(self, coordinator, appliance: Mapping[str, object]) -> None:
        super().__init__(coordinator, appliance, "microwave_cook_time")
        self.entity_description = NumberEntityDescription(
            key="microwave_cook_time",
            translation_key="microwave_cook_time",
            icon="mdi:timer",
            device_class=NumberDeviceClass.DURATION,
            native_min_value=1,
            native_max_value=120,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.MINUTES,
            mode=NumberMode.BOX,
            suggested_display_precision=0,
        )
        self._attr_name = entity_name_from_key("microwave_cook_time", appliance)

    @property
    def native_value(self) -> float | None:
        options = microwave_local_options(self.coordinator, self.said)
        if options.get("cook_time_seconds") is not None:
            return max(1, round(float(options["cook_time_seconds"]) / 60))
        value = _seconds_to_minutes(_seconds_value(self.flat_status, "Mwo_TimeSetCookTimeSet"))
        return max(1, round(float(value))) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        minutes = max(1, round(float(value)))
        update_microwave_options(self.coordinator, self.said, cook_time_seconds=minutes * 60)
        self.async_write_ha_state()


class WhirlpoolMicrowaveNumber(WhirlpoolApkEntity, NumberEntity):
    """Microwave non-time pending option number."""

    def __init__(self, coordinator, appliance: Mapping[str, object], key: str, option_key: str) -> None:
        self.option_key = option_key
        super().__init__(coordinator, appliance, key)

        if key == "microwave_cook_power":
            desc = NumberEntityDescription(
                key=key,
                translation_key=key,
                icon="mdi:lightning-bolt",
                native_min_value=1,
                native_max_value=100,
                native_step=1,
                native_unit_of_measurement=PERCENTAGE,
                mode=NumberMode.SLIDER,
                suggested_display_precision=0,
            )
        elif key == "microwave_amount":
            desc = NumberEntityDescription(
                key=key,
                translation_key=key,
                icon="mdi:scale",
                native_min_value=0,
                native_max_value=65535,
                native_step=1,
                mode=NumberMode.BOX,
                suggested_display_precision=0,
            )
        else:
            desc = NumberEntityDescription(
                key=key,
                translation_key=key,
                icon="mdi:thermometer",
                device_class=NumberDeviceClass.TEMPERATURE,
                native_min_value=0,
                native_max_value=300,
                native_step=1,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                mode=NumberMode.BOX,
                suggested_display_precision=0,
            )

        self.entity_description = desc
        self._attr_name = entity_name_from_key(key, appliance)

    @property
    def native_value(self) -> float | None:
        options = microwave_local_options(self.coordinator, self.said)
        if self.option_key in options and options[self.option_key] is not None:
            return round(float(options[self.option_key])) if self.option_key == "target_temperature" else float(options[self.option_key])

        attr = {
            "cook_power": "Mwo_CycleSetCookPower",
            "amount": "Mwo_CycleSetAmount",
            "target_temperature": "Mwo_CycleSetTargetTemp",
        }[self.option_key]
        raw = _int_attr(self.flat_status, attr)
        if raw is None:
            return None
        if self.option_key == "target_temperature":
            return round(raw / 10)
        return raw

    async def async_set_native_value(self, value: float) -> None:
        if self.option_key == "target_temperature":
            update_microwave_options(self.coordinator, self.said, target_temperature=round(float(value)))
        elif self.option_key == "cook_power":
            update_microwave_options(self.coordinator, self.said, cook_power=max(1, min(100, int(value))))
        else:
            update_microwave_options(self.coordinator, self.said, **{self.option_key: int(value)})
        self.async_write_ha_state()
