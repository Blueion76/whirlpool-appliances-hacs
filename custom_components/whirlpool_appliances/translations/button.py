"""Button entities for Whirlpool Appliances integration."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, entity_name_from_key, is_cooking_appliance, microwave_exists, oven_cavity_exists


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


# Do not expose the legacy /api/v1/appliance/{said}/sync endpoint as a button.
# On Minerva cooking appliances Whirlpool returns 404 for that endpoint, even
# though /api/v1/appliance/{said} and STOMP updates work. Keep the method
# available for diagnostics/services, but avoid a broken entity in HA.
BUTTONS = (
    WhirlpoolButtonDescription(key="refresh_status", translation_key="refresh_status", press_fn=_refresh_status),
    WhirlpoolButtonDescription(key="sync_time", translation_key="sync_time", press_fn=_sync_time),
)

THING_BUTTONS = (
    WhirlpoolButtonDescription(key="request_thing_state", translation_key="request_thing_state", press_fn=_request_thing_state),
)

COOKING_BUTTONS = (
    WhirlpoolButtonDescription(key="stop_upper_oven", translation_key="stop_upper_oven", press_fn=_stop_upper_oven),
    WhirlpoolButtonDescription(key="stop_lower_oven", translation_key="stop_lower_oven", press_fn=_stop_lower_oven),
    WhirlpoolButtonDescription(key="stop_microwave", translation_key="stop_microwave", press_fn=_stop_microwave),
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
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            has_mwo = microwave_exists(flat)
            for desc in COOKING_BUTTONS:
                if desc.key.startswith("stop_upper") and not oven_cavity_exists(flat, "upper"):
                    continue
                if desc.key.startswith("stop_lower") and not oven_cavity_exists(flat, "lower"):
                    continue
                if desc.key == "stop_microwave" and not has_mwo:
                    continue
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
        self._check_service_request(await self.entity_description.press_fn(target, self.said))
        await self.coordinator.async_request_refresh()
