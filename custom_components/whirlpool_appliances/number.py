"""Number entities for Whirlpool setpoints and non-time options."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import PERCENTAGE, UnitOfTemperature
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


def _allowed_oven_temperatures() -> tuple[float, ...]:
    """Return Whirlpool oven setpoints in native Celsius units.

    Whirlpool exposes oven temperatures as Celsius/tenths of Celsius, while
    common oven setpoints are 5 °F increments. Keep the native values in
    Celsius so Home Assistant can convert them for display.
    """
    return tuple(round(_fahrenheit_to_celsius(v), 1) for v in range(175, 551, 5))


def _snap_oven_temperature(value: float) -> float:
    allowed = _allowed_oven_temperatures()
    return min(allowed, key=lambda allowed_value: abs(allowed_value - float(value)))


def _int_attr(flat: Mapping[str, Any], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    if raw in (None, ""):
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


async def _send_oven_options(entity: WhirlpoolApkEntity, cavity: str, options: Mapping[str, Any]) -> None:
    """Apply oven options when the oven is already running."""
    active = oven_is_active(entity.flat_status, cavity)
    raise_if_common_blocked(entity.flat_status, cavity=cavity)
    if active and frozen_or_custom_cycle(entity.flat_status, cavity):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="modify_not_allowed")
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
        "Applying Whirlpool oven attrs from temperature number entity: entity=%s said=%s cavity=%s attrs=%s",
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
    """Set up non-time Whirlpool number entities."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = []

    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance) or not is_cooking_appliance(appliance):
            continue

        flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
        if oven_cavity_exists(flat, "upper"):
            entities.append(WhirlpoolTargetTemperatureNumber(coordinator, appliance, "upper"))
        if oven_cavity_exists(flat, "lower"):
            entities.append(WhirlpoolTargetTemperatureNumber(coordinator, appliance, "lower"))

        entities.append(WhirlpoolDisplayBrightnessNumber(coordinator, appliance))

        if microwave_exists(flat):
            entities.extend(
                (
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_cook_power", "cook_power"),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_amount", "amount"),
                    WhirlpoolMicrowaveNumber(coordinator, appliance, "microwave_target_temperature", "target_temperature"),
                )
            )

    async_add_entities(entities)


class WhirlpoolTargetTemperatureNumber(WhirlpoolApkEntity, NumberEntity):
    """Pending oven target temperature selector."""

    def __init__(self, coordinator, appliance: Mapping[str, object], cavity: str | None) -> None:
        self.cavity = cavity
        suffix = f"{cavity}_target_temperature_setpoint" if cavity else "target_temperature_setpoint"
        super().__init__(coordinator, appliance, suffix)
        self.entity_description = NumberEntityDescription(
            key=suffix,
            translation_key=suffix,
            device_class=NumberDeviceClass.TEMPERATURE,
            native_min_value=_allowed_oven_temperatures()[0],
            native_max_value=_allowed_oven_temperatures()[-1],
            native_step=0.1,
            native_unit_of_measurement=UnitOfTemperature.CELSIUS,
            mode=NumberMode.BOX,
        )
        self._attr_suggested_display_precision = 0
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> float | None:
        local = local_options(self.coordinator, self.said, self.cavity)
        if "target_temp" in local:
            return _snap_oven_temperature(float(local["target_temp"]))

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

        return _snap_oven_temperature(value)

    async def async_set_native_value(self, value: float) -> None:
        celsius = _snap_oven_temperature(value)
        if self.cavity in ("upper", "lower") and oven_is_active(self.flat_status, self.cavity):
            options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
            options["target_temp"] = celsius
            await _send_oven_options(self, self.cavity, options)
            return
        update_local_options(self.coordinator, self.said, self.cavity, target_temp=celsius)
        self.async_write_ha_state()


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
            )
        else:
            desc = NumberEntityDescription(
                key=key,
                translation_key=key,
                icon="mdi:thermometer",
                device_class=NumberDeviceClass.TEMPERATURE,
                native_min_value=0,
                native_max_value=300,
                native_step=0.1,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                mode=NumberMode.BOX,
            )

        self.entity_description = desc
        if self.option_key == "target_temperature":
            self._attr_suggested_display_precision = 0
        self._attr_name = entity_name_from_key(key, appliance)

    @property
    def native_value(self) -> float | None:
        options = microwave_local_options(self.coordinator, self.said)
        if self.option_key in options and options[self.option_key] is not None:
            value = float(options[self.option_key])
            return value

        attr = {
            "cook_power": "Mwo_CycleSetCookPower",
            "amount": "Mwo_CycleSetAmount",
            "target_temperature": "Mwo_CycleSetTargetTemp",
        }[self.option_key]
        raw = _int_attr(self.flat_status, attr)
        if raw is None:
            return None
        if self.option_key == "target_temperature":
            return raw / 10
        return raw

    async def async_set_native_value(self, value: float) -> None:
        if self.option_key == "target_temperature":
            update_microwave_options(
                self.coordinator,
                self.said,
                target_temperature=round(float(value), 1),
            )
        elif self.option_key == "cook_power":
            update_microwave_options(self.coordinator, self.said, cook_power=max(1, min(100, int(value))))
        else:
            update_microwave_options(self.coordinator, self.said, **{self.option_key: int(value)})
        self.async_write_ha_state()
