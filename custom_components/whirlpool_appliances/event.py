"""Event entities for Whirlpool Appliances integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import WhirlpoolApkConfigEntry
from .const import DOMAIN
from .coordinator import WhirlpoolApkCoordinator
from .helpers.logging import summarize

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL_SECONDS = 300


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WhirlpoolApkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Whirlpool event entities."""
    async_add_entities([WhirlpoolMessageCenterEvent(entry.runtime_data)])


class WhirlpoolMessageCenterEvent(CoordinatorEntity[WhirlpoolApkCoordinator], EventEntity):
    """Account-level event entity for Whirlpool message-center notifications."""

    _attr_has_entity_name = True
    _attr_name = "Message center"
    _attr_translation_key = "message_center"
    _attr_icon = "mdi:bell-badge"
    _attr_event_types = ["message"]

    def __init__(self, coordinator: WhirlpoolApkCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_message_center"
        self._seen_ids: set[str] = set()
        self._seeded = False
        self._unsub_timer: CALLBACK_TYPE | None = None
        self._refreshing = False

    async def async_added_to_hass(self) -> None:
        """Start polling message-center state."""
        await super().async_added_to_hass()
        await self._async_check_messages(seed_only=True)
        self._unsub_timer = async_track_time_interval(
            self.hass,
            self._schedule_check,
            self.coordinator.update_interval,
        )
        self.async_on_remove(self._remove_timer)

    @callback
    def _remove_timer(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
            self._unsub_timer = None

    @callback
    def _schedule_check(self, _now) -> None:
        self.hass.async_create_task(self._async_check_messages())

    async def _async_check_messages(self, *, seed_only: bool = False) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            messages = await self._async_messages()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Whirlpool message-center refresh failed: %s", err)
            self._refreshing = False
            return

        new_messages: list[Mapping[str, Any]] = []
        current_ids: set[str] = set()
        for message in messages:
            message_id = _message_id(message)
            if not message_id:
                continue
            current_ids.add(message_id)
            if self._seeded and message_id not in self._seen_ids:
                new_messages.append(message)

        self._seen_ids.update(current_ids)
        if seed_only:
            self._seeded = True
            self._refreshing = False
            return

        self._seeded = True
        for message in new_messages:
            event_attributes = _event_attributes(message)
            _LOGGER.debug("Whirlpool message-center event: %s", summarize(event_attributes))
            self._trigger_event("message", event_attributes)
            self.async_write_ha_state()
        self._refreshing = False

    async def _async_messages(self) -> list[Mapping[str, Any]]:
        client = self.coordinator.client
        if not client.user_id:
            await client._populate_user_details()  # noqa: SLF001 - existing profile lookup helper
        if not client.user_id:
            return []
        result = await client.request("GET", f"/api/v1/users/{client.user_id}/messages")
        return _coerce_messages(result)


def _coerce_messages(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        for key in ("messages", "items", "data", "notifications", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, Mapping)]
        return [payload]
    return []


def _message_id(message: Mapping[str, Any]) -> str | None:
    for key in ("messageId", "message_id", "id", "notificationId", "notification_id"):
        value = message.get(key)
        if value not in (None, "", 0):
            return str(value)
    return None


def _event_attributes(message: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "message_id": _message_id(message),
        "title": _first(message, "title", "subject", "header", "name"),
        "message": _first(message, "message", "body", "description", "text"),
        "type": _first(message, "type", "notificationType", "category"),
        "created_at": _first(message, "createdAt", "created_at", "timestamp", "date"),
        "raw": dict(message),
    }


def _first(message: Mapping[str, Any], *keys: str) -> Any | None:
    for key in keys:
        value = message.get(key)
        if value not in (None, ""):
            return value
    return None
