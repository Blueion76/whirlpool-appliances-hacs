"""Time entities for Whirlpool duration-style timer controls."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, entity_name_from_key, is_cooking_appliance, microwave_exists, oven_cavity_exists
from .helpers.control import microwave_local_options, update_microwave_options
from .helpers.oven_options import local_options, update_local_options


SECONDS_PER_DAY = 24 * 60 * 60


def _seconds_to_time(seconds: int | float | None) -> time | None:
    if seconds is None:
        return None
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return None
    total = max(0, min(SECONDS_PER_DAY - 1, total))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return time(hour=hours, minute=minutes, second=secs)


def _time_to_seconds(value: time | None) -> int | None:
    if value is None:
        return None
    return int(value.hour) * 3600 + int(value.minute) * 60 + int(value.second)


def _seconds_attr(flat: Mapping[str, Any], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    if raw in (None, ""):
        return None
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _cavity_prefix(cavity: str) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    """Set up Whirlpool time selector entities."""
    coordinator = entry.runtime_data
    entities: list[TimeEntity] = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance) or not is_cooking_appliance(appliance):
            continue
        flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
        if oven_cavity_exists(flat, "upper"):
            entities.append(WhirlpoolOvenDurationTime(coordinator, appliance, "upper", "cook_time"))
            entities.append(WhirlpoolOvenDurationTime(coordinator, appliance, "upper", "delay_time"))
        if oven_cavity_exists(flat, "lower"):
            entities.append(WhirlpoolOvenDurationTime(coordinator, appliance, "lower", "cook_time"))
            entities.append(WhirlpoolOvenDurationTime(coordinator, appliance, "lower", "delay_time"))
        entities.append(WhirlpoolKitchenTimerTime(coordinator, appliance))
        if microwave_exists(flat):
            entities.append(WhirlpoolMicrowaveDurationTime(coordinator, appliance))
    async_add_entities(entities)


class WhirlpoolOvenDurationTime(WhirlpoolApkEntity, TimeEntity):
    """HH:MM:SS selector for oven cook/delay durations.

    Home Assistant's time selector stores a time-of-day object. For Whirlpool
    timers we interpret that value as an elapsed duration from midnight.
    """

    _attr_icon = "mdi:timer"

    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str, kind: str) -> None:
        self.cavity = cavity
        self.kind = kind
        suffix = f"{cavity}_oven_{kind}_time"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> time | None:
        local = local_options(self.coordinator, self.said, self.cavity)
        local_key = "cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes"
        if local_key in local:
            return _seconds_to_time(float(local[local_key]) * 60)
        prefix = _cavity_prefix(self.cavity)
        attr = f"{prefix}_TimeSetCookTimeSet" if self.kind == "cook_time" else f"{prefix}_TimeSetDelayTime"
        return _seconds_to_time(_seconds_attr(self.flat_status, attr))

    async def async_set_value(self, value: time) -> None:
        seconds = _time_to_seconds(value) or 0
        minutes = seconds / 60
        update_local_options(
            self.coordinator,
            self.said,
            self.cavity,
            **{"cook_time_minutes" if self.kind == "cook_time" else "delay_time_minutes": minutes},
        )
        self.async_write_ha_state()


class WhirlpoolMicrowaveDurationTime(WhirlpoolApkEntity, TimeEntity):
    """HH:MM:SS selector for microwave cook time."""

    _attr_icon = "mdi:timer"

    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "microwave_cook_time_time")
        self._attr_name = entity_name_from_key("microwave_cook_time_time", appliance)

    @property
    def native_value(self) -> time | None:
        options = microwave_local_options(self.coordinator, self.said)
        if options.get("cook_time_seconds") is not None:
            return _seconds_to_time(options["cook_time_seconds"])
        return _seconds_to_time(_seconds_attr(self.flat_status, "Mwo_TimeSetCookTimeSet"))

    async def async_set_value(self, value: time) -> None:
        seconds = max(1, _time_to_seconds(value) or 0)
        update_microwave_options(self.coordinator, self.said, cook_time_seconds=seconds)
        self.async_write_ha_state()


class WhirlpoolKitchenTimerTime(WhirlpoolApkEntity, TimeEntity):
    """HH:MM:SS selector for kitchen timer 1."""

    _attr_icon = "mdi:timer"

    def __init__(self, coordinator, appliance: Mapping[str, Any]) -> None:
        super().__init__(coordinator, appliance, "kitchen_timer_1_time")
        self._attr_name = entity_name_from_key("kitchen_timer_1_time", appliance)

    @property
    def native_value(self) -> time | None:
        return _seconds_to_time(_seconds_attr(self.flat_status, "KitchenTimer01_SetTimeSet"))

    async def async_set_value(self, value: time) -> None:
        seconds = _time_to_seconds(value) or 0
        self._check_service_request(await self.client.set_kitchen_timer(self.said, seconds, 1))
        await self.coordinator.async_request_refresh()
