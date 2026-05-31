"""Button entities for Whirlpool Appliances integration."""
from __future__ import annotations

import logging

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .const import DOMAIN
from .helpers.control import (
    microwave_attrs,
    microwave_local_options,
    oven_cook_attrs,
    oven_is_active,
    raise_if_common_blocked,
)
from .entity import WhirlpoolApkEntity, entity_name_from_key, is_aircon_appliance, is_cooking_appliance, microwave_exists, oven_cavity_exists
from .helpers.logging import summarize
from .helpers.oven_options import current_oven_options, minutes_to_seconds

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class WhirlpoolButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[Any, str], Awaitable[Any]]


async def _refresh_status(client, said: str):
    return await client.get_status(said)


async def _sync_time(client, said: str):
    return await client.sync_appliance_time(said)


async def _request_thing_state(coordinator, said: str):
    return await coordinator.async_publish_thing_command(said, "getState")


async def _stop_upper_oven(client, said: str):
    return await client.stop_oven_cavity(said, "upper")


async def _stop_lower_oven(client, said: str):
    return await client.stop_oven_cavity(said, "lower")


async def _stop_microwave(client, said: str):
    return await client.stop_microwave(said)


async def _start_upper_oven(entity, said: str):
    return await entity.async_start_oven()


async def _start_lower_oven(entity, said: str):
    return await entity.async_start_oven()


async def _start_microwave(entity, said: str):
    return await entity.async_start_microwave()


async def _stop_kitchen_timer(client, said: str):
    return await client.stop_kitchen_timer(said, 1)


async def _check_firmware_update(client, said: str):
    return await client.check_firmware_update(said)


async def _reset_ac_filter(client, said: str):
    return await client.request("POST", "/api/v1/ac/resetACFilter", json={"said": said, "saId": said})


BUTTONS = (
    WhirlpoolButtonDescription(key="refresh_status", translation_key="refresh_status", icon="mdi:cloud-refresh-variant", press_fn=_refresh_status),
    WhirlpoolButtonDescription(key="sync_time", translation_key="sync_time", icon="mdi:cloud-clock", press_fn=_sync_time),
    WhirlpoolButtonDescription(key="stop_kitchen_timer_1", translation_key="stop_kitchen_timer_1", icon="mdi:timer-stop", press_fn=_stop_kitchen_timer),
    WhirlpoolButtonDescription(key="check_firmware_update", translation_key="check_firmware_update", icon="mdi:cloud-refresh-variant", press_fn=_check_firmware_update),
)

THING_BUTTONS = (
    WhirlpoolButtonDescription(key="request_thing_state", translation_key="request_thing_state", press_fn=_request_thing_state),
)

AC_BUTTONS = (
    WhirlpoolButtonDescription(key="reset_ac_filter", translation_key="reset_ac_filter", icon="mdi:air-filter", press_fn=_reset_ac_filter),
)

COOKING_BUTTONS = (
    WhirlpoolButtonDescription(key="start_upper_oven", translation_key="start_upper_oven", icon="mdi:play", press_fn=_start_upper_oven),
    WhirlpoolButtonDescription(key="start_lower_oven", translation_key="start_lower_oven", icon="mdi:play", press_fn=_start_lower_oven),
    WhirlpoolButtonDescription(key="stop_upper_oven", translation_key="stop_upper_oven", icon="mdi:stop", press_fn=_stop_upper_oven),
    WhirlpoolButtonDescription(key="stop_lower_oven", translation_key="stop_lower_oven", icon="mdi:stop", press_fn=_stop_lower_oven),
    WhirlpoolButtonDescription(key="start_microwave", translation_key="start_microwave", icon="mdi:play", press_fn=_start_microwave),
    WhirlpoolButtonDescription(key="stop_microwave", translation_key="stop_microwave", icon="mdi:stop", press_fn=_stop_microwave),
)


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        is_thing = bool(appliance.get("thingShield")) or str(appliance.get("source") or "").upper() == "TS_SAID"
        if is_thing:
            for desc in THING_BUTTONS:
                entities.append(WhirlpoolApkButton(coordinator, appliance, desc, use_coordinator=True))
            continue
        for desc in BUTTONS:
            entities.append(WhirlpoolApkButton(coordinator, appliance, desc))
        if is_aircon_appliance(appliance):
            for desc in AC_BUTTONS:
                entities.append(WhirlpoolApkButton(coordinator, appliance, desc))
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            has_mwo = microwave_exists(flat)
            for desc in COOKING_BUTTONS:
                if (desc.key.startswith("start_upper") or desc.key.startswith("stop_upper")) and not oven_cavity_exists(flat, "upper"):
                    continue
                if (desc.key.startswith("start_lower") or desc.key.startswith("stop_lower")) and not oven_cavity_exists(flat, "lower"):
                    continue
                if desc.key in {"start_microwave", "stop_microwave"} and not has_mwo:
                    continue
                if desc.key.startswith("start_upper") or desc.key.startswith("start_lower"):
                    cavity = "lower" if "lower" in desc.key else "upper"
                    entities.append(WhirlpoolStartOvenButton(coordinator, appliance, desc, cavity))
                elif desc.key == "start_microwave":
                    entities.append(WhirlpoolStartMicrowaveButton(coordinator, appliance, desc))
                else:
                    entities.append(WhirlpoolApkButton(coordinator, appliance, desc))
    async_add_entities(entities)


class WhirlpoolApkButton(WhirlpoolApkEntity, ButtonEntity):
    entity_description: WhirlpoolButtonDescription

    def __init__(self, coordinator, appliance: Mapping[str, Any], description: WhirlpoolButtonDescription, *, use_coordinator: bool = False) -> None:
        super().__init__(coordinator, appliance, description.key)
        self.entity_description = description
        self._use_coordinator = use_coordinator
        self._attr_name = entity_name_from_key(description.translation_key or description.key, appliance)

    async def async_press(self) -> None:
        target = self.coordinator if self._use_coordinator else self.client
        _LOGGER.debug("Pressing Whirlpool button: entity=%s said=%s key=%s", self.entity_id, self.said, self.entity_description.key)
        result = await self.entity_description.press_fn(target, self.said)
        _LOGGER.debug("Whirlpool button result: entity=%s key=%s result=%s", self.entity_id, self.entity_description.key, summarize(result))
        self._check_service_request(result)
        await self.coordinator.async_request_refresh()


class WhirlpoolStartOvenButton(WhirlpoolApkButton):
    """Start or modify oven using locally selected temp/mode/timing/preset options."""

    def __init__(self, coordinator, appliance: Mapping[str, Any], description: WhirlpoolButtonDescription, cavity: str) -> None:
        self.cavity = cavity
        super().__init__(coordinator, appliance, description)
        if cavity == "upper" and not oven_cavity_exists(self.flat_status, "lower"):
            self._attr_name = "Start Oven"

    async def async_press(self) -> None:
        _LOGGER.debug("Pressing Whirlpool Start Oven button: entity=%s said=%s cavity=%s", self.entity_id, self.said, self.cavity)
        result = await self.async_start_oven()
        _LOGGER.debug("Whirlpool Start Oven result: entity=%s cavity=%s result=%s", self.entity_id, self.cavity, summarize(result))
        self._check_service_request(result)
        await self.coordinator.async_request_refresh()

    async def async_start_oven(self) -> Any:
        options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
        active = oven_is_active(self.flat_status, self.cavity)
        _LOGGER.debug("Whirlpool Start Oven options: said=%s cavity=%s options=%s active=%s", self.said, self.cavity, summarize(options), active)

        # Keep only reliable local safety checks. Do not veto oven start/modify
        # based on stale cycle/timing state; let the Whirlpool cloud/appliance
        # accept or reject the same payload used by the oven_control service.
        raise_if_common_blocked(self.flat_status, cavity=self.cavity)

        if options.get("frozen_food"):
            if active:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="modify_not_allowed")
            return await self.client.set_oven_frozen_bake(
                self.said,
                str(options["frozen_food"]),
                float(options["target_temp"]),
                minutes_to_seconds(options.get("cook_time_minutes") or 10) or 600,
                self.cavity,
                complete_action=str(options["complete_action"]),
            )

        attrs = oven_cook_attrs(
            cavity=self.cavity,
            temperature=float(options["target_temp"]),
            mode=str(options["mode"]),
            cook_time_seconds=minutes_to_seconds(options.get("cook_time_minutes")),
            delay_time_seconds=minutes_to_seconds(options.get("delay_time_minutes")),
            complete_action=str(options["complete_action"]),
            operation="4" if active else "2",
            flat_status=self.flat_status,
        )
        time_attrs = {key: attrs.pop(key) for key in list(attrs) if key.endswith(("_TimeSetCookTimeSet", "_TimeSetDelayTime"))}
        if time_attrs:
            _LOGGER.debug("Applying Whirlpool oven time attributes before operation: said=%s cavity=%s attrs=%s", self.said, self.cavity, summarize(time_attrs))
            self._check_service_request(await self.client.send_attributes(self.said, time_attrs))
        _LOGGER.debug("Final Whirlpool oven %s attributes: said=%s cavity=%s attrs=%s", "modify" if active else "start", self.said, self.cavity, summarize(attrs))
        return await self.client.send_attributes(self.said, attrs)


class WhirlpoolStartMicrowaveButton(WhirlpoolApkButton):
    """Start microwave using locally selected mode/time/power options."""

    async def async_press(self) -> None:
        _LOGGER.debug("Pressing Whirlpool Start Microwave button: entity=%s said=%s", self.entity_id, self.said)
        result = await self.async_start_microwave()
        _LOGGER.debug("Whirlpool Start Microwave result: entity=%s result=%s", self.entity_id, summarize(result))
        self._check_service_request(result)
        await self.coordinator.async_request_refresh()

    async def async_start_microwave(self) -> Any:
        raise_if_common_blocked(self.flat_status, microwave=True)
        options = microwave_local_options(self.coordinator, self.said)
        attrs = microwave_attrs(options)
        operation_attrs = {"Mwo_OperationSetOperations": attrs.pop("Mwo_OperationSetOperations", "2")}

        if attrs:
            _LOGGER.debug(
                "Applying Whirlpool microwave settings before start: said=%s options=%s attrs=%s",
                self.said,
                summarize(options),
                summarize(attrs),
            )
            self._check_service_request(await self.client.send_attributes(self.said, attrs))

        _LOGGER.debug(
            "Applying Whirlpool microwave start operation: said=%s attrs=%s",
            self.said,
            summarize(operation_attrs),
        )
        return await self.client.send_attributes(self.said, operation_attrs)
