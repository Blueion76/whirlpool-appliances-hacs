"""Experimental legacy SAID WebSocket/STOMP push support for Whirlpool appliances."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from aiohttp import ClientWebSocketResponse, WSMsgType

from .api import WhirlpoolApiError, WhirlpoolCloudClient, appliance_said
from .helpers.logging import summarize, summarize_keys

_LOGGER = logging.getLogger(__name__)

StateCallback = Callable[[str, dict[str, Any]], Awaitable[None] | None]

_STOMP_PROTOCOLS = ("v12.stomp", "v11.stomp", "v10.stomp")
_HEARTBEAT = "10000,10000"


def _candidate_ws_urls(client: WhirlpoolCloudClient) -> list[str]:
    """Return candidate legacy WebSocket/STOMP URLs for the selected region."""
    root = client.base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1).rstrip("/")
    paths = (
        "/ws",
        "/websocket",
        "/stomp",
        "/api/v1/ws",
        "/api/v1/websocket",
        "/api/v1/stomp",
        "/api/v1/stomp/websocket",
        "/api/v1/appliance/ws",
        "/api/v1/appliance/websocket",
        "/api/v1/appliance/status/ws",
        "/api/v1/appliance/status/websocket",
    )
    return [f"{root}{path}" for path in paths]


def _legacy_destinations(client: WhirlpoolCloudClient, saids: list[str]) -> list[str]:
    """Return broad STOMP destinations used by Whirlpool mobile app generations."""
    destinations: list[str] = []
    for value in (client.account_id, client.user_id):
        if value:
            destinations.extend(
                (
                    f"/topic/account/{value}",
                    f"/topic/accounts/{value}",
                    f"/topic/user/{value}",
                    f"/topic/users/{value}",
                    f"/user/{value}/queue/status",
                    f"/user/{value}/queue/appliance",
                )
            )
    for said in saids:
        destinations.extend(
            (
                f"/topic/said/{said}",
                f"/topic/appliance/{said}",
                f"/topic/appliances/{said}",
                f"/topic/appliance/{said}/status",
                f"/topic/appliances/{said}/status",
                f"/queue/appliance/{said}",
                f"/user/queue/appliance/{said}",
                f"/app/appliance/{said}",
            )
        )
    result: list[str] = []
    for destination in destinations:
        if destination not in result:
            result.append(destination)
    return result


def _frame(command: str, headers: Mapping[str, Any] | None = None, body: str = "") -> str:
    lines = [command]
    for key, value in (headers or {}).items():
        if value is not None:
            lines.append(f"{key}:{value}")
    lines.append("")
    return "\n".join(lines) + "\n" + body + "\x00"


def _parse_frames(raw: str) -> list[tuple[str, dict[str, str], str]]:
    frames: list[tuple[str, dict[str, str], str]] = []
    for part in raw.split("\x00"):
        part = part.strip("\n\r")
        if not part:
            continue
        head, _, body = part.partition("\n\n")
        lines = head.splitlines()
        if not lines:
            continue
        command = lines[0].strip()
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip()] = value.strip()
        frames.append((command, headers, body))
    return frames


def _json_body(body: str) -> Any:
    body = body.strip()
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"payload": body}


def _extract_said(payload: Any, headers: Mapping[str, str], known_saids: set[str]) -> str | None:
    """Best-effort SAID lookup from STOMP headers/destination/body."""
    for value in headers.values():
        text = str(value)
        for said in known_saids:
            if said and said in text:
                return said

    candidate_keys = {
        "said",
        "SAID",
        "saId",
        "applianceId",
        "applianceID",
        "applianceSAID",
        "serialNumber",
        "serial_number",
        "id",
    }

    def walk(value: Any) -> str | None:
        if isinstance(value, Mapping):
            for key in candidate_keys:
                item = value.get(key)
                if item not in (None, "", 0):
                    text = str(item)
                    if not known_saids or text in known_saids:
                        return text
                    for said in known_saids:
                        if said and said in text:
                            return said
            for item in value.values():
                found = walk(item)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return None

    return walk(payload)


def _status_payload(payload: Any) -> dict[str, Any]:
    """Normalize variable socket payload shapes into a status dict."""
    if isinstance(payload, Mapping):
        for key in ("status", "applianceStatus", "state", "payload", "data", "attributes"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                merged = dict(value)
                merged.setdefault("pushEnvelope", {k: v for k, v in payload.items() if k != key})
                return merged
        return dict(payload)
    return {"payload": payload}


class WhirlpoolLegacyStompManager:
    """Best-effort legacy SAID WebSocket/STOMP subscriber.

    This is intentionally non-fatal. If Whirlpool moves/rejects the legacy socket
    endpoint, normal REST polling continues to work. When a candidate endpoint does
    accept STOMP messages, updates are merged into the coordinator immediately.
    """

    def __init__(self, client: WhirlpoolCloudClient, state_callback: StateCallback) -> None:
        self.client = client
        self._state_callback = state_callback
        self._saids: list[str] = []
        self.states: dict[str, dict[str, Any]] = {}
        self.connected = False
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._ws: ClientWebSocketResponse | None = None

    async def ensure_started(self, appliances: list[Mapping[str, Any]]) -> None:
        """Start the background subscriber for legacy SAID appliances."""
        saids = self._legacy_saids(appliances)
        if not saids:
            await self.shutdown()
            return
        self._saids = saids
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="whirlpool_legacy_stomp")

    async def shutdown(self) -> None:
        """Stop the background subscriber."""
        self._stop.set()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._ws = None
        self.connected = False

    async def ensure_connected(self) -> None:
        """Restart the subscriber if it exited unexpectedly."""
        if self._saids and (not self._task or self._task.done()):
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="whirlpool_legacy_stomp")

    async def _run(self) -> None:
        backoff = 5
        while not self._stop.is_set():
            try:
                await self.client.ensure_auth_valid()
                await self._connect_once()
                backoff = 5
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                self.connected = False
                self.last_error = str(err)
                _LOGGER.debug("Whirlpool legacy STOMP push disconnected/failed: %s", err)
            if not self._stop.is_set():
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _connect_once(self) -> None:
        errors: list[str] = []
        for url in _candidate_ws_urls(self.client):
            if self._stop.is_set():
                return
            try:
                await self._connect_url(url)
                return
            except Exception as err:  # noqa: BLE001
                errors.append(f"{url}: {err}")
                _LOGGER.debug("Whirlpool legacy STOMP candidate failed: %s error=%s", url, err)
        raise WhirlpoolApiError("No Whirlpool legacy STOMP endpoint accepted connection: " + "; ".join(errors[-3:]))

    async def _connect_url(self, url: str) -> None:
        headers = self.client._headers(auth=True)  # noqa: SLF001 - reuse mobile auth headers
        headers["Authorization"] = f"Bearer {self.client.access_token}"
        _LOGGER.debug("Connecting Whirlpool legacy STOMP candidate: %s", url)
        async with self.client.session.ws_connect(
            url,
            headers=headers,
            protocols=_STOMP_PROTOCOLS,
            heartbeat=30,
            timeout=20,
            autoclose=True,
            autoping=True,
        ) as ws:
            self._ws = ws
            await self._connect_stomp(ws)
            await self._listen(ws)

    async def _connect_stomp(self, ws: ClientWebSocketResponse) -> None:
        headers = {
            "accept-version": "1.2,1.1,1.0",
            "heart-beat": _HEARTBEAT,
            "authorization": f"Bearer {self.client.access_token}",
            "Authorization": f"Bearer {self.client.access_token}",
            "accountId": self.client.account_id,
            "userId": self.client.user_id,
        }
        await ws.send_str(_frame("CONNECT", headers))
        msg = await ws.receive(timeout=20)
        if msg.type != WSMsgType.TEXT:
            raise WhirlpoolApiError(f"Expected STOMP CONNECTED text frame, got {msg.type}")
        frames = _parse_frames(msg.data)
        if not frames or frames[0][0] != "CONNECTED":
            raise WhirlpoolApiError(f"STOMP CONNECT failed: {msg.data[:500]}")
        self.connected = True
        self.last_error = None
        _LOGGER.info("Connected Whirlpool legacy STOMP push")
        await self._subscribe(ws)

    async def _subscribe(self, ws: ClientWebSocketResponse) -> None:
        for idx, destination in enumerate(_legacy_destinations(self.client, self._saids), start=1):
            headers = {
                "id": f"whirlpool-{idx}",
                "destination": destination,
                "ack": "auto",
            }
            await ws.send_str(_frame("SUBSCRIBE", headers))
            _LOGGER.debug("Subscribed Whirlpool legacy STOMP destination: %s", destination)

    async def _listen(self, ws: ClientWebSocketResponse) -> None:
        known_saids = set(self._saids)
        while not self._stop.is_set():
            msg = await ws.receive(timeout=75)
            if msg.type == WSMsgType.TEXT:
                for command, headers, body in _parse_frames(msg.data):
                    if command == "MESSAGE":
                        await self._handle_message(headers, body, known_saids)
                    elif command == "ERROR":
                        raise WhirlpoolApiError(f"STOMP ERROR: {body[:500]}")
                    elif command in {"CONNECTED", "RECEIPT"}:
                        continue
                    else:
                        _LOGGER.debug("Whirlpool legacy STOMP frame ignored: command=%s headers=%s", command, headers)
            elif msg.type == WSMsgType.CLOSED:
                raise WhirlpoolApiError("STOMP websocket closed")
            elif msg.type == WSMsgType.ERROR:
                raise WhirlpoolApiError(f"STOMP websocket error: {ws.exception()}")
            elif msg.type in (WSMsgType.PING, WSMsgType.PONG):
                continue
            elif msg.type == WSMsgType.CLOSE:
                raise WhirlpoolApiError("STOMP websocket close frame received")

    async def _handle_message(self, headers: Mapping[str, str], body: str, known_saids: set[str]) -> None:
        payload = _json_body(body)
        said = _extract_said(payload, headers, known_saids)
        if not said:
            _LOGGER.debug("Whirlpool legacy STOMP message missing SAID: headers=%s payload=%s", headers, summarize(payload))
            return
        state = _status_payload(payload)
        state.setdefault("source", "stomp")
        state.setdefault("stompHeaders", dict(headers))
        state.setdefault("stompReceivedAt", int(time.time()))
        _LOGGER.debug("Whirlpool legacy STOMP state update: said=%s shape=%s payload=%s", said, summarize_keys(state), summarize(state))
        self.states[said] = state
        result = self._state_callback(said, state)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    def _legacy_saids(appliances: list[Mapping[str, Any]]) -> list[str]:
        result: list[str] = []
        for appliance in appliances:
            said = appliance_said(appliance)
            if not said:
                continue
            source = str(appliance.get("source") or appliance.get("applianceType") or "").upper()
            if bool(appliance.get("thingShield")) or source == "TS_SAID":
                continue
            if said not in result:
                result.append(said)
        return result
