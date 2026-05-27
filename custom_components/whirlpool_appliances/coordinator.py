"""Coordinator for Whirlpool Appliances integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WhirlpoolApiError, WhirlpoolCloudClient, appliance_ddm_key, appliance_said
from .api_mqtt import WhirlpoolThingShieldManager
from .helpers.capabilities import parse_ddm_capabilities
from .helpers.logging import summarize, summarize_keys

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


def _find_ddm_key_from_status(status: Any) -> str | None:
    """Best-effort DDM key lookup from a raw status/appliance snapshot."""
    if not isinstance(status, Mapping):
        return None

    candidates = (
        "ddmKey",
        "DDM_KEY",
        "dataModelKey",
        "DATA_MODEL_KEY",
        "data_model_key",
        "dataModel",
        "DATA_MODEL",
    )

    def walk(value: Any) -> str | None:
        if isinstance(value, Mapping):
            for key in candidates:
                found = value.get(key)
                if found not in (None, "", "0", 0):
                    return str(found)
            # Legacy status sometimes nests metadata under an Appliance object.
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(status)


def _appliance_category(appliance: Mapping[str, Any], status: Any | None = None) -> str | None:
    """Return the most useful category/type string for diagnostics."""
    keys = (
        "CATEGORY_NAME",
        "categoryName",
        "category",
        "Category",
        "applianceCategory",
        "applianceType",
        "type",
    )
    for key in keys:
        value = appliance.get(key)
        if value not in (None, "", "0", 0):
            return str(value)

    if isinstance(status, Mapping):
        for key in keys:
            value = status.get(key)
            if value not in (None, "", "0", 0):
                return str(value)
    return None


def _appliance_metadata(appliance: Mapping[str, Any], status: Any | None = None) -> dict[str, Any]:
    """Return normalized appliance metadata used by device info and diagnostics."""
    status_map = status if isinstance(status, Mapping) else {}
    return {
        "said": appliance_said(appliance),
        "ddm_key": appliance_ddm_key(appliance) or _find_ddm_key_from_status(status),
        "category": _appliance_category(appliance, status),
        "model": (
            appliance.get("MODEL_NO")
            or appliance.get("modelNumber")
            or appliance.get("model_number")
            or appliance.get("model")
            or status_map.get("ModelNumber")
        ),
        "serial": (
            appliance.get("SERIAL")
            or appliance.get("serialNumber")
            or appliance.get("serial")
            or status_map.get("SerialNumber")
        ),
        "ccuri": (
            appliance.get("ccuri")
            or appliance.get("CC_URI")
            or status_map.get("ccuri")
            or status_map.get("CC_URI")
        ),
        "data_model_key": (
            appliance.get("DATA_MODEL_KEY")
            or appliance.get("dataModelKey")
            or status_map.get("DATA_MODEL_KEY")
        ),
        "source": appliance.get("source"),
        "thing_shield": bool(appliance.get("thingShield")),
    }



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
        self._ddm_capabilities: dict[str, Any] = {}
        self._ddm_errors: dict[str, str] = {}
        self._appliance_metadata: dict[str, dict[str, Any]] = {}

    async def async_start_push(self) -> None:
        """Start ThingShield MQTT subscriptions for discovered TS_SAID devices."""
        saids = self._thing_saids(self._latest_appliances)
        if not saids:
            return
        try:
            _LOGGER.debug("Starting Whirlpool ThingShield MQTT for %d appliance(s): %s", len(saids), sorted(saids))
            await self.thing_manager.ensure_started(saids, self._latest_appliances)
            self._thing_started = True
            self._merge_thing_metadata()
            _LOGGER.debug("Whirlpool ThingShield MQTT startup complete")
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
        _LOGGER.debug("Publishing Whirlpool ThingShield command: said=%s command=%s payload=%s", said, command, summarize(payload))
        result = await self.thing_manager.publish_command(said, command, payload)
        _LOGGER.debug("Published Whirlpool ThingShield command result: said=%s command=%s result=%s", said, command, summarize(result))
        return result

    async def async_fetch_ddm_capabilities(
        self,
        appliances: list[dict[str, Any]] | None = None,
        statuses: Mapping[str, Any] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Fetch DDM/capability payloads for all discovered appliance data-model keys."""
        appliances = appliances if appliances is not None else self._latest_appliances
        statuses = statuses if statuses is not None else self._latest_statuses

        _LOGGER.debug("Refreshing Whirlpool DDM capabilities: appliance_count=%d force=%s", len(appliances), force)
        ddm_keys: dict[str, dict[str, Any]] = {}
        for appliance in appliances:
            said = appliance_said(appliance)
            status = statuses.get(said) if said and isinstance(statuses, Mapping) else None
            key = appliance_ddm_key(appliance) or _find_ddm_key_from_status(status)
            if not key:
                continue
            ddm_keys.setdefault(
                key,
                {
                    "ddm_key": key,
                    "appliance_saids": [],
                    "category": _appliance_category(appliance, status),
                    "model": (
                        appliance.get("MODEL_NO")
                        or appliance.get("modelNumber")
                        or appliance.get("model")
                    ),
                },
            )
            if said and said not in ddm_keys[key]["appliance_saids"]:
                ddm_keys[key]["appliance_saids"].append(said)

        for key, metadata in ddm_keys.items():
            if not force and key in self._ddm_capabilities:
                _LOGGER.debug("Using cached Whirlpool DDM capabilities: key=%s metadata=%s", key, summarize(metadata))
                continue
            try:
                first_said = next((str(s) for s in metadata.get("appliance_saids", []) if s), None)
                _LOGGER.debug("Fetching Whirlpool DDM capabilities: key=%s said=%s metadata=%s", key, first_said, summarize(metadata))
                payload = await self.client.get_ddm_capabilities(key, said=first_said, force=force)
            except WhirlpoolApiError as err:
                self._ddm_errors[key] = str(err)
                _LOGGER.warning("DDM capability fetch failed for %s: %s", key, err)
                continue
            parsed = parse_ddm_capabilities(payload)
            _LOGGER.debug(
                "Parsed Whirlpool DDM capabilities: key=%s schema=%s attributes=%s features=%s cooking=%s",
                key,
                parsed.get("schema"),
                parsed.get("attribute_count"),
                summarize(parsed.get("supported_features")),
                summarize_keys(parsed.get("cooking")),
            )
            self._ddm_capabilities[key] = {
                "metadata": metadata,
                "parsed": parsed,
                "payload": payload,
            }
            self._ddm_errors.pop(key, None)

        return self._ddm_capabilities

    def _build_appliance_metadata(
        self,
        appliances: list[dict[str, Any]],
        statuses: Mapping[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Build normalized metadata for every discovered appliance."""
        metadata: dict[str, dict[str, Any]] = {}
        for appliance in appliances:
            said = appliance_said(appliance)
            if not said:
                continue
            metadata[said] = _appliance_metadata(appliance, statuses.get(said))
        self._appliance_metadata = metadata
        return metadata

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            _LOGGER.debug("Starting Whirlpool coordinator refresh")
            if not self.client.authenticated:
                _LOGGER.debug("Whirlpool client is not authenticated; logging in before refresh")
                await self.client.login()
            appliances = await self.client.list_appliances()
            _LOGGER.debug("Whirlpool appliance list refreshed: count=%d payload=%s", len(appliances), summarize(appliances))
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
                    _LOGGER.debug("Whirlpool legacy snapshot updated: said=%s shape=%s", said, summarize_keys(snapshot))
                    statuses[said] = snapshot
                else:
                    _LOGGER.debug("Whirlpool legacy snapshot unavailable: said=%s", said)
                    statuses[said] = {"error": "No usable Whirlpool REST status/appliance snapshot returned"}
            self._latest_statuses = statuses
            appliance_metadata = self._build_appliance_metadata(appliances, statuses)
            await self.async_fetch_ddm_capabilities(appliances, statuses)
            _LOGGER.debug(
                "Completed Whirlpool coordinator refresh: appliances=%d statuses=%d ddm=%d ddm_errors=%d",
                len(appliances),
                len(statuses),
                len(self._ddm_capabilities),
                len(self._ddm_errors),
            )
            return {
                "appliances": appliances,
                "statuses": statuses,
                "appliance_metadata": appliance_metadata,
                "ddm_capabilities": self._ddm_capabilities,
                "ddm_errors": self._ddm_errors,
            }
        except WhirlpoolApiError as err:
            _LOGGER.warning("Whirlpool coordinator refresh failed: %s", err)
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
                _LOGGER.debug("Optional REST snapshot failed: said=%s getter=%s error=%s", said, getter.__name__, err)
                continue
            _LOGGER.debug("Optional REST snapshot succeeded: said=%s getter=%s shape=%s", said, getter.__name__, summarize_keys(data))
            if isinstance(data, Mapping):
                merged.update(data)
            else:
                merged.setdefault("payload", data)
            got_any = True
        _LOGGER.debug("Merged Whirlpool REST snapshot: said=%s got_any=%s shape=%s", said, got_any, summarize_keys(merged))
        return merged if got_any else None

    def _handle_thing_state(self, said: str, state: dict[str, Any]) -> None:
        """Merge a MQTT state update into the coordinator on the HA event loop."""
        _LOGGER.debug("Received Whirlpool ThingShield state update: said=%s shape=%s payload=%s", said, summarize_keys(state), summarize(state))
        def apply() -> None:
            statuses = dict(self._latest_statuses)
            statuses[said] = state
            self._latest_statuses = statuses
            data = dict(self.data or {})
            data["statuses"] = statuses
            data.setdefault("appliances", self._latest_appliances)
            data.setdefault("appliance_metadata", self._appliance_metadata)
            data.setdefault("ddm_capabilities", self._ddm_capabilities)
            data.setdefault("ddm_errors", self._ddm_errors)
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
