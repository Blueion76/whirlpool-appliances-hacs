"""Button entities for Whirlpool Appliances integration."""
from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_ddm_key, appliance_said
from .capabilities import cooking_cavity_capability
from .entity import WhirlpoolApkEntity, entity_name_from_key, is_cooking_appliance, microwave_exists, oven_cavity_exists
from .oven_options import current_oven_options, oven_is_active


@dataclass(frozen=True, kw_only=True)
class WhirlpoolButtonDescription(ButtonEntityDescription):
    press_fn: Callable[[Any, str], Awaitable[Any]]


async def _sync(client, said: str):
    return await client.sync_appliance(said)


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


async def _stop_kitchen_timer(client, said: str):
    return await client.stop_kitchen_timer(said, 1)


async def _check_firmware_update(client, said: str):
    return await client.check_firmware_update(said)


# Do not expose the legacy /api/v1/appliance/{said}/sync endpoint as a button.
# On Minerva cooking appliances Whirlpool returns 404 for that endpoint, even
# though /api/v1/appliance/{said} and STOMP updates work. Keep the method
# available for diagnostics/services, but avoid a broken entity in HA.
BUTTONS = (
    WhirlpoolButtonDescription(key="refresh_status", translation_key="refresh_status", icon="mdi:cloud-refresh-variant", press_fn=_refresh_status),
    WhirlpoolButtonDescription(key="sync_time", translation_key="sync_time", icon="mdi:cloud-clock", press_fn=_sync_time),
    WhirlpoolButtonDescription(key="stop_kitchen_timer_1", translation_key="stop_kitchen_timer_1", icon="mdi:timer-stop", press_fn=_stop_kitchen_timer),
    WhirlpoolButtonDescription(key="check_firmware_update", translation_key="check_firmware_update", icon="mdi:cloud-refresh-variant", press_fn=_check_firmware_update),
)

THING_BUTTONS = (
    WhirlpoolButtonDescription(key="request_thing_state", translation_key="request_thing_state", press_fn=_request_thing_state),
)

COOKING_BUTTONS = (
    WhirlpoolButtonDescription(key="start_upper_oven", translation_key="start_upper_oven", icon="mdi:play", press_fn=_start_upper_oven),
    WhirlpoolButtonDescription(key="start_lower_oven", translation_key="start_lower_oven", icon="mdi:play", press_fn=_start_lower_oven),
    WhirlpoolButtonDescription(key="stop_upper_oven", translation_key="stop_upper_oven", icon="mdi:stop", press_fn=_stop_upper_oven),
    WhirlpoolButtonDescription(key="stop_lower_oven", translation_key="stop_lower_oven", icon="mdi:stop", press_fn=_stop_lower_oven),
    WhirlpoolButtonDescription(key="stop_microwave", translation_key="stop_microwave", icon="mdi:stop", press_fn=_stop_microwave),
)


def _parsed_ddm_for_appliance(coordinator, appliance: Mapping[str, Any]) -> Mapping[str, Any] | None:
    data = coordinator.data or {}
    ddm_key = appliance_ddm_key(appliance) or appliance.get("DATA_MODEL_KEY") or appliance.get("dataModelKey")
    capabilities = data.get("ddm_capabilities") or {}
    if ddm_key and isinstance(capabilities.get(ddm_key), Mapping):
        parsed = capabilities[ddm_key].get("parsed")
        if isinstance(parsed, Mapping):
            return parsed
    return None


def _frozen_bake_foods(coordinator, appliance: Mapping[str, Any], cavity: str) -> list[Mapping[str, Any]]:
    capability = cooking_cavity_capability(_parsed_ddm_for_appliance(coordinator, appliance), cavity)
    if not isinstance(capability, Mapping):
        return []
    frozen = capability.get("frozen_bake")
    if not isinstance(frozen, Mapping):
        return []
    foods = frozen.get("foods")
    return [food for food in foods if isinstance(food, Mapping)] if isinstance(foods, list) else []


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
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            has_mwo = microwave_exists(flat)
            for desc in COOKING_BUTTONS:
                if (desc.key.startswith("start_upper") or desc.key.startswith("stop_upper")) and not oven_cavity_exists(flat, "upper"):
                    continue
                if (desc.key.startswith("start_lower") or desc.key.startswith("stop_lower")) and not oven_cavity_exists(flat, "lower"):
                    continue
                if desc.key == "stop_microwave" and not has_mwo:
                    continue
                if desc.key.startswith("start_"):
                    cavity = "lower" if "lower" in desc.key else "upper"
                    entities.append(WhirlpoolStartOvenButton(coordinator, appliance, desc, cavity))
                else:
                    entities.append(WhirlpoolApkButton(coordinator, appliance, desc))
            for cavity in ("upper", "lower"):
                if not oven_cavity_exists(flat, cavity):
                    continue
                for food in _frozen_bake_foods(coordinator, appliance, cavity):
                    entities.append(WhirlpoolFrozenBakeButton(coordinator, appliance, cavity, food))
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
        self._check_service_request(await self.entity_description.press_fn(target, self.said))
        await self.coordinator.async_request_refresh()



class WhirlpoolStartOvenButton(WhirlpoolApkButton):
    """Start oven using locally selected temp/mode/timing/preset options."""

    def __init__(self, coordinator, appliance: Mapping[str, Any], description: WhirlpoolButtonDescription, cavity: str) -> None:
        self.cavity = cavity
        super().__init__(coordinator, appliance, description)

    async def async_press(self) -> None:
        self._check_service_request(await self.async_start_oven())
        await self.coordinator.async_request_refresh()

    async def async_start_oven(self) -> Any:
        options = current_oven_options(self.coordinator, self.said, self.cavity, self.flat_status)
        if oven_is_active(self.flat_status, self.cavity):
            self._check_service_request(await self.client.stop_oven_cavity(self.said, self.cavity))
            await asyncio.sleep(1)

        if options.get("frozen_food"):
            return await self.client.set_oven_frozen_bake(
                self.said,
                str(options["frozen_food"]),
                float(options["target_temp"]),
                int(options.get("cook_time_seconds") or 600),
                self.cavity,
                complete_action=str(options["complete_action"]),
            )

        return await self.client.set_oven_cook(
            self.said,
            float(options["target_temp"]),
            str(options["mode"]),
            self.cavity,
            cook_time_seconds=options.get("cook_time_seconds"),
            delay_time_seconds=options.get("delay_time_seconds"),
            complete_action=str(options["complete_action"]),
        )



class WhirlpoolFrozenBakeButton(WhirlpoolApkEntity, ButtonEntity):
    """Start a Frozen Bake food cycle with DDM default temperature/time."""

    def __init__(self, coordinator, appliance: Mapping[str, Any], cavity: str, food: Mapping[str, Any]) -> None:
        self.cavity = cavity
        self.food = str(food.get("food"))
        temp = food.get("target_temperature") if isinstance(food.get("target_temperature"), Mapping) else {}
        cook_time = food.get("cook_time") if isinstance(food.get("cook_time"), Mapping) else {}
        self.temperature_c = float(temp.get("default_c") or 204.4)
        self.cook_time_seconds = int(cook_time.get("default") or cook_time.get("min") or 600)
        suffix = f"{cavity}_frozen_bake_{self.food}"
        super().__init__(coordinator, appliance, suffix)
        self._attr_name = entity_name_from_key(suffix, appliance)
        self._attr_translation_key = suffix
        self._attr_icon = "mdi:snowflake"

    async def async_press(self) -> None:
        self._check_service_request(
            await self.client.set_oven_frozen_bake(
                self.said,
                self.food,
                self.temperature_c,
                self.cook_time_seconds,
                self.cavity,
            )
        )
        await self.coordinator.async_request_refresh()
