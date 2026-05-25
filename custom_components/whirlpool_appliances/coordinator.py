"""Coordinator for Whirlpool Appliances integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WhirlpoolApiError, WhirlpoolCloudClient, appliance_said
from .api_mqtt import WhirlpoolThingShieldManager

_LOGGER = logging.getLogger(__name__)


def _has_substantive_state(state: Any) -> bool:
    """True when a status payload contains more than presence/pending metadata."""
    if not isinstance(state, Mapping):
        return bool(state)
    metadata_keys = {
        "online",
        "source",
        "pending",
        "detail",
        "mqttTopic",
        "mqttRaw",
        "topicModel",
        "lastResponse",
        "lastRequestId",
        "error",
    }
    return any(key not in metadata_keys for key in state)


class WhirlpoolApkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Keep Whirlpool appliance list and status data fresh.

    Legacy SAID appliances are refreshed through REST polling. ThingShield TS_SAID
    appliances are subscribed through AWS IoT MQTT and merged into the same status map.
    """

    def __init__(self, hass, client: WhirlpoolCloudClient, update_interval: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Whirlpool Appliances appliances",
            update_interval=timedelta(seconds=update_interval),
        )
        self.client = client
        self.thing_manager = WhirlpoolThingShieldManager(client, self._handle_thing_state)
        self._thing_started = False
        self._latest_appliances: list[dict[str, Any]] = []
        self._latest_statuses: dict[str, Any] = {}

    async def async_start_push(self) -> None:
        """Start ThingShield MQTT subscriptions for discovered TS_SAID devices."""
        saids = self._thing_saids(self._latest_appliances)
        if not saids:
            return
        try:
            await self.thing_manager.ensure_started(saids, self._latest_appliances)
            self._thing_started = True
            self._merge_thing_metadata()
        except WhirlpoolApiError as err:
            _LOGGER.warning("Whirlpool ThingShield MQTT startup failed: %s", err)

    async def async_shutdown(self) -> None:
        """Disconnect realtime connections on unload."""
        await self.thing_manager.shutdown()

    async def async_publish_thing_command(
        self, said: str, command: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Publish a command to a ThingShield appliance."""
        if said not in self.thing_manager.runtimes:
            await self.async_start_push()
        return await self.thing_manager.publish_command(said, command, payload)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            if not self.client.authenticated:
                await self.client.login()
            appliances = await self.client.list_appliances()
            self._latest_appliances = appliances
            self._merge_thing_metadata()
            statuses: dict[str, Any] = dict(self._latest_statuses)

            thing_saids = set(self._thing_saids(appliances))
            if thing_saids:
                # Keep MQTT credentials/connection healthy and ask for fresh snapshots.
                if self._thing_started:
                    try:
                        await self.thing_manager.ensure_connected()
                    except WhirlpoolApiError as err:
                        _LOGGER.debug("ThingShield reconnect failed during refresh: %s", err)
                else:
                    await self.async_start_push()
                statuses.update(self.thing_manager.states)

            for appliance in appliances:
                said = appliance_said(appliance)
                if not said:
                    continue
                if said in thing_saids:
                    mqtt_state = self.thing_manager.states.get(said)
                    if mqtt_state and _has_substantive_state(mqtt_state):
                        statuses[said] = mqtt_state
                        continue
                    # Some TS_SAID devices still expose a useful REST snapshot.
                    # Try it as a non-fatal fallback while waiting for MQTT state.
                    rest_snapshot = await self._try_rest_snapshot(said)
                    if rest_snapshot is not None:
                        merged = dict(mqtt_state or {})
                        merged.update(rest_snapshot if isinstance(rest_snapshot, dict) else {"restSnapshot": rest_snapshot})
                        merged.setdefault("source", "mqtt+rest")
                        statuses[said] = merged
                    else:
                        statuses.setdefault(
                            said,
                            mqtt_state
                            or {
                                "source": "mqtt",
                                "pending": True,
                                "detail": "MQTT connected; waiting for a state/update or command response payload",
                            },
                        )
                    continue
                snapshot = await self._try_legacy_snapshot(said)
                if snapshot is not None:
                    statuses[said] = snapshot
                else:
                    statuses[said] = {"error": "No usable Whirlpool REST status/appliance snapshot returned"}
            self._latest_statuses = statuses
            return {"appliances": appliances, "statuses": statuses}
        except WhirlpoolApiError as err:
            raise UpdateFailed(str(err)) from err

    async def _try_rest_snapshot(self, said: str) -> Any | None:
        """Best-effort REST snapshot for TS_SAID devices."""
        return await self._try_legacy_snapshot(said)

    async def _try_legacy_snapshot(self, said: str) -> Any | None:
        """Best-effort full REST snapshot for legacy SAID appliances.

        MizterB/whirlpool-sixth-sense fetches legacy appliance data from
        /api/v1/appliance/{said}; Minerva cooking appliances expose their useful
        values under the returned attributes map, not only under /status/{said}.
        Merge both endpoints when both work, but make /api/v1/appliance/{said}
        the authoritative source for attributes.
        """
        merged: dict[str, Any] = {}
        got_any = False
        for getter in (self.client.get_status, self.client.get_appliance):
            try:
                data = await getter(said)
            except WhirlpoolApiError as err:
                _LOGGER.debug("Optional REST snapshot failed for %s: %s", said, err)
                continue
            if isinstance(data, Mapping):
                merged.update(data)
            else:
                merged.setdefault("payload", data)
            got_any = True
        return merged if got_any else None

    def _handle_thing_state(self, said: str, state: dict[str, Any]) -> None:
        """Merge a MQTT state update into the coordinator on the HA event loop."""
        def apply() -> None:
            statuses = dict(self._latest_statuses)
            statuses[said] = state
            self._latest_statuses = statuses
            data = dict(self.data or {})
            data["statuses"] = statuses
            data.setdefault("appliances", self._latest_appliances)
            self.async_set_updated_data(data)

        self.hass.loop.call_soon_threadsafe(apply)

    @staticmethod
    def _thing_saids(appliances: list[dict[str, Any]]) -> list[str]:
        saids: list[str] = []
        for appliance in appliances:
            said = appliance_said(appliance)
            if not said:
                continue
            source = str(appliance.get("source") or appliance.get("applianceType") or "").upper()
            thing_flag = bool(appliance.get("thingShield"))
            if thing_flag or source == "TS_SAID":
                saids.append(said)
        return saids

    def _merge_thing_metadata(self) -> None:
        """Update appliance list entries with discovered AWS IoT metadata."""
        if not self._latest_appliances:
            return
        by_said = {said: runtime for said, runtime in self.thing_manager.runtimes.items()}
        if not by_said:
            return
        updated: list[dict[str, Any]] = []
        for appliance in self._latest_appliances:
            said = appliance_said(appliance)
            runtime = by_said.get(said or "")
            if not runtime:
                updated.append(appliance)
                continue
            info = runtime.info
            enriched = dict(appliance)
            enriched["model"] = runtime.model
            enriched["thingShield"] = True
            enriched["source"] = "TS_SAID"
            if info:
                enriched.update(
                    {
                        "name": info.name,
                        "brand": info.brand,
                        "category": info.category,
                        "serialNumber": info.serial or said,
                        "thingId": info.thing_id,
                    }
                )
            updated.append(enriched)
        self._latest_appliances = updated
