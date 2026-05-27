"""Coordinator for Whirlpool Appliances integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import WhirlpoolApiError, WhirlpoolCloudClient, appliance_ddm_key, appliance_said
from .api_mqtt import WhirlpoolThingShieldManager
from .api_stomp import WhirlpoolLegacyStompManager
from .helpers.capabilities import parse_ddm_capabilities
from .helpers.logging import summarize, summarize_keys

_LOGGER = logging.getLogger(__name__)


def _has_substantive_state(state: Any) -> bool:
    """Return true if a push payload contains meaningful state data."""
    if not isinstance(state, Mapping):
        return False
    ignored = {"source", "stompHeaders", "stompReceivedAt", "pushEnvelope"}
    return any(key not in ignored and value not in (None, "", {}, []) for key, value in state.items())


def _appliance_category(appliance: Mapping[str, Any], status: Any = None) -> str | None:
    for key in ("category", "applianceCategory", "SAID_TYPE", "applianceType", "type"):
        value = appliance.get(key)
        if value:
            return str(value)
    if isinstance(status, Mapping):
        for key in ("category", "applianceCategory", "SAID_TYPE", "applianceType", "type"):
            value = status.get(key)
            if value:
                return str(value)
    return None


def _find_ddm_key_from_status(status: Any) -> str | None:
    """Best-effort DDM key lookup from status payloads."""
    if not isinstance(status, Mapping):
        return None
    for key in (
        "dataModelKey",
        "DATA_MODEL_KEY",
        "ddmKey",
        "deviceDataModelKey",
        "DeviceDataModelKey",
    ):
        value = status.get(key)
        if value:
            return str(value)
    attributes = status.get("attributes")
    if isinstance(attributes, Mapping):
        for key in (
            "dataModelKey",
            "DATA_MODEL_KEY",
            "ddmKey",
            "deviceDataModelKey",
            "DeviceDataModelKey",
        ):
            value = attributes.get(key)
            if isinstance(value, Mapping):
                value = value.get("value")
            if value:
                return str(value)
    return None


def _merge_status(existing: Any, update: Any) -> Any:
    """Merge an incremental status payload into an existing status payload."""
    if isinstance(existing, Mapping) and isinstance(update, Mapping):
        merged = dict(existing)
        for key, value in update.items():
            if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
                merged[key] = _merge_status(merged[key], value)
            else:
                merged[key] = value
        return merged
    return update if update is not None else existing


class WhirlpoolApkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Whirlpool Appliances data coordinator."""

    def __init__(self, hass, client: WhirlpoolCloudClient, scan_interval: int) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Whirlpool Appliances",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.thing_manager = WhirlpoolThingShieldManager(client, self._handle_thing_state)
        self.legacy_push_manager = WhirlpoolLegacyStompManager(client, self._handle_legacy_push_state)
        self._thing_started = False
        self._legacy_push_started = False
        self._latest_appliances: list[dict[str, Any]] = []
        self._latest_statuses: dict[str, Any] = {}
        self._ddm_capabilities: dict[str, Any] = {}
        self._ddm_errors: dict[str, str] = {}
        self._appliance_metadata: dict[str, dict[str, Any]] = {}

    async def async_start_push(self) -> None:
        """Start realtime subscriptions for discovered appliances."""
        saids = self._thing_saids(self._latest_appliances)
        if saids:
            try:
                _LOGGER.debug(
                    "Starting Whirlpool ThingShield MQTT for %d appliance(s): %s",
                    len(saids),
                    sorted(saids),
                )
                await self.thing_manager.ensure_started(saids, self._latest_appliances)
                self._thing_started = True
                self._merge_thing_metadata()
                _LOGGER.debug("Whirlpool ThingShield MQTT startup complete")
            except WhirlpoolApiError as err:
                _LOGGER.warning("Whirlpool ThingShield MQTT startup failed: %s", err)

        legacy_saids = self._legacy_saids(self._latest_appliances)
        if legacy_saids:
            try:
                _LOGGER.debug(
                    "Starting Whirlpool legacy STOMP push for %d appliance(s): %s",
                    len(legacy_saids),
                    sorted(legacy_saids),
                )
                await self.legacy_push_manager.ensure_started(self._latest_appliances)
                self._legacy_push_started = True
            except WhirlpoolApiError as err:
                _LOGGER.warning("Whirlpool legacy STOMP push startup failed: %s", err)

    async def async_shutdown(self) -> None:
        """Disconnect realtime connections on unload."""
        await self.thing_manager.shutdown()
        await self.legacy_push_manager.shutdown()

    async def async_publish_thing_command(
        self, said: str, command: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Publish a command to a ThingShield appliance."""
        if said not in self.thing_manager.runtimes:
            await self.async_start_push()
        _LOGGER.debug(
            "Publishing Whirlpool ThingShield command: said=%s command=%s payload=%s",
            said,
            command,
            summarize(payload),
        )
        result = await self.thing_manager.publish_command(said, command, payload)
        _LOGGER.debug(
            "Published Whirlpool ThingShield command result: said=%s command=%s result=%s",
            said,
            command,
            summarize(result),
        )
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

        _LOGGER.debug(
            "Refreshing Whirlpool DDM capabilities: appliance_count=%d force=%s",
            len(appliances),
            force,
        )
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
                _LOGGER.debug(
                    "Using cached Whirlpool DDM capabilities: key=%s metadata=%s",
                    key,
                    summarize(metadata),
                )
                continue
            try:
                first_said = next((str(s) for s in metadata.get("appliance_saids", []) if s), None)
                _LOGGER.debug(
                    "Fetching Whirlpool DDM capabilities: key=%s said=%s metadata=%s",
                    key,
                    first_said,
                    summarize(metadata),
                )
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

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch account appliances and statuses."""
        try:
            appliances = await self.client.list_appliances()
            self._latest_appliances = list(appliances)
            self._merge_thing_metadata()
            statuses: dict[str, Any] = {}

            if self._thing_started:
                await self.thing_manager.ensure_connected()
            if self._legacy_push_started:
                await self.legacy_push_manager.ensure_connected()

            for said, push_state in self.legacy_push_manager.states.items():
                if self.legacy_push_manager.connected and _has_substantive_state(push_state):
                    statuses[said] = push_state

            for appliance in appliances:
                said = appliance_said(appliance)
                if not said:
                    continue
                if said in statuses:
                    continue
                source = str(appliance.get("source") or appliance.get("applianceType") or "").upper()
                if bool(appliance.get("thingShield")) or source == "TS_SAID":
                    state = self.thing_manager.states.get(said)
                    if state:
                        statuses[said] = state
                        continue
                status = await self.client.get_status(said)
                statuses[said] = status

            for said, existing in self._latest_statuses.items():
                if said not in statuses and existing is not None:
                    statuses[said] = existing

            for appliance in appliances:
                said = appliance_said(appliance)
                if not said:
                    continue
                push_state = self.legacy_push_manager.states.get(said)
                if self.legacy_push_manager.connected and push_state and _has_substantive_state(push_state):
                    statuses[said] = _merge_status(statuses.get(said), push_state)

            self._latest_statuses = statuses
            await self.async_fetch_ddm_capabilities(appliances, statuses)
            data = {
                "appliances": appliances,
                "statuses": statuses,
                "ddm_capabilities": self._ddm_capabilities,
                "ddm_errors": self._ddm_errors,
                "appliance_metadata": self._appliance_metadata,
                "thing_states": self.thing_manager.states,
                "legacy_push_states": self.legacy_push_manager.states,
                "legacy_push_connected": self.legacy_push_manager.connected,
            }
            _LOGGER.debug(
                "Whirlpool coordinator updated: appliances=%d statuses=%s ddm_keys=%s legacy_push_connected=%s",
                len(appliances),
                sorted(statuses),
                sorted(self._ddm_capabilities),
                self.legacy_push_manager.connected,
            )
            return data
        except WhirlpoolApiError as err:
            raise UpdateFailed(str(err)) from err

    @staticmethod
    def _thing_saids(appliances: list[Mapping[str, Any]]) -> set[str]:
        """Return SAIDs that are ThingShield devices."""
        saids: set[str] = set()
        for appliance in appliances:
            said = appliance_said(appliance)
            if not said:
                continue
            source = str(appliance.get("source") or appliance.get("applianceType") or "").upper()
            if bool(appliance.get("thingShield")) or source == "TS_SAID":
                saids.add(said)
        return saids

    @staticmethod
    def _legacy_saids(appliances: list[Mapping[str, Any]]) -> set[str]:
        """Return SAIDs that should use legacy STOMP push."""
        saids: set[str] = set()
        for appliance in appliances:
            said = appliance_said(appliance)
            if not said:
                continue
            source = str(appliance.get("source") or appliance.get("applianceType") or "").upper()
            if bool(appliance.get("thingShield")) or source == "TS_SAID":
                continue
            saids.add(said)
        return saids

    def _merge_thing_metadata(self) -> None:
        """Copy available ThingShield runtime metadata into latest appliance records."""
        runtimes = getattr(self.thing_manager, "runtimes", {}) or {}
        explicit_metadata = getattr(self.thing_manager, "metadata", {}) or {}
        for appliance in self._latest_appliances:
            said = appliance_said(appliance)
            if not said:
                continue
            metadata: dict[str, Any] = dict(explicit_metadata.get(said) or {})
            runtime = runtimes.get(said)
            if runtime is not None:
                info = getattr(runtime, "info", None)
                metadata.setdefault("model", getattr(runtime, "model", None))
                metadata.setdefault("model_hints", list(getattr(runtime, "model_hints", ()) or ()))
                if info is not None:
                    for key in ("model", "brand", "category", "serial", "name", "thing_id"):
                        value = getattr(info, key, None)
                        if value not in (None, "", [], {}):
                            metadata[key] = value
            metadata = {key: value for key, value in metadata.items() if value not in (None, "", [], {})}
            if metadata:
                appliance.setdefault("thing_metadata", metadata)
                self._appliance_metadata[said] = metadata

    def _handle_thing_state(self, said: str, state: dict[str, Any]) -> None:
        """Handle ThingShield state callback."""
        self._latest_statuses[said] = state
        self._merge_thing_metadata()
        data = dict(self.data or {})
        statuses = dict(data.get("statuses") or {})
        statuses[said] = state
        data["statuses"] = statuses
        data["thing_states"] = self.thing_manager.states
        data["appliance_metadata"] = self._appliance_metadata
        self.async_set_updated_data(data)

    def _handle_legacy_push_state(self, said: str, state: dict[str, Any]) -> None:
        """Handle legacy STOMP state callback."""
        merged = _merge_status(self._latest_statuses.get(said), state)
        self._latest_statuses[said] = merged
        data = dict(self.data or {})
        statuses = dict(data.get("statuses") or {})
        statuses[said] = _merge_status(statuses.get(said), state)
        data["statuses"] = statuses
        data["legacy_push_states"] = self.legacy_push_manager.states
        data["legacy_push_connected"] = self.legacy_push_manager.connected
        self.async_set_updated_data(data)
