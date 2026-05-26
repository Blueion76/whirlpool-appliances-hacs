"""ThingShield AWS IoT MQTT support for Whirlpool appliances.

This is adapted from the public reverse-engineered ``bassrock/hass-whirlpool``
implementation, generalized for the APK-derived multi-appliance integration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from .api import ThingInfo, WhirlpoolApiError, WhirlpoolCloudClient, WhirlpoolConnectionError

_LOGGER = logging.getLogger(__name__)

MQTT_KEEPALIVE = 30
CONNECTION_TIMEOUT = 15.0

StateCallback = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class ThingRuntime:
    """Runtime metadata for one ThingShield appliance."""

    said: str
    model: str
    info: ThingInfo | None = None
    model_hints: tuple[str, ...] = ()


class WhirlpoolThingShieldMqttClient:
    """Low-level AWS IoT MQTT client using SigV4 websocket auth."""

    def __init__(self, client: WhirlpoolCloudClient, on_message: Callable[[str, dict[str, Any]], None]) -> None:
        self._client = client
        self._on_message = on_message
        self._connection: Any | None = None
        self._connected = False
        self._client_id = ""

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def client_id(self) -> str:
        return self._client_id

    async def connect(self) -> None:
        """Connect to AWS IoT using Cognito-derived temporary credentials."""
        if self._connected:
            return
        identity_id, credentials = await self._client.get_aws_credentials()
        self._client_id = f"{identity_id}_{uuid.uuid4().hex[:16]}"
        loop = asyncio.get_running_loop()

        def _connect() -> Any:
            from awscrt import auth
            from awsiot import mqtt_connection_builder

            provider = auth.AwsCredentialsProvider.new_static(
                access_key_id=credentials.access_key,
                secret_access_key=credentials.secret_key,
                session_token=credentials.session_token,
            )
            connection = mqtt_connection_builder.websockets_with_default_aws_signing(
                endpoint=self._client.iot_endpoint,
                region=self._client.aws_region,
                credentials_provider=provider,
                client_id=self._client_id,
                clean_session=True,
                keep_alive_secs=MQTT_KEEPALIVE,
                on_connection_interrupted=self._on_connection_interrupted,
                on_connection_resumed=self._on_connection_resumed,
            )
            connection.connect().result(timeout=CONNECTION_TIMEOUT)
            return connection

        try:
            self._connection = await loop.run_in_executor(None, _connect)
        except Exception as err:  # noqa: BLE001
            self._connected = False
            raise WhirlpoolConnectionError(f"AWS IoT MQTT connection failed: {err}") from err
        self._connected = True
        _LOGGER.info("Connected to Whirlpool ThingShield MQTT as %s", self._client_id)

    async def disconnect(self) -> None:
        """Disconnect from MQTT."""
        if self._connection is None:
            return
        connection = self._connection
        self._connection = None
        self._connected = False
        loop = asyncio.get_running_loop()
        try:
            future = connection.disconnect()
            await loop.run_in_executor(None, lambda: future.result(timeout=CONNECTION_TIMEOUT))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Ignoring MQTT disconnect error: %s", err)

    async def subscribe_appliance(self, runtime: ThingRuntime) -> None:
        """Subscribe to state, command response, presence, OTA, and capability topics.

        The Android app builds most topics with the AWS IoT thing type/model, but
        some accounts expose a different model string in the cloud appliance list
        than in ``DescribeThing``.  Exact topics are still subscribed for the normal
        path, and wildcard subscriptions catch state/response frames when the
        topic model segment differs.  Presence topics are model-independent, which
        is why a device can show "Connected" while all state sensors remain
        unknown if we only listen to exact model topics.
        """
        from awscrt import mqtt

        if self._connection is None or not self._connected:
            raise WhirlpoolConnectionError("MQTT is not connected")

        model_candidates = [runtime.model, *runtime.model_hints]
        topics: list[str] = [
            f"$aws/events/presence/connected/{runtime.said}",
            f"$aws/events/presence/disconnected/{runtime.said}",
            # Wildcards keep state working when the thing type returned by AWS IoT
            # differs from the topic model used by the appliance firmware.
            f"cmd/+/{runtime.said}/response/{self._client_id}",
            f"cmd/+/{runtime.said}/response/#",
            f"dt/+/{runtime.said}/state/update",
            f"dt/+/{runtime.said}/state/#",
            f"api/capability/download/+/{runtime.said}/response",
            f"dt/+/{runtime.said}/ota/status",
        ]
        for model in model_candidates:
            if not model or model == "Unknown":
                continue
            topics.extend(
                [
                    f"cmd/{model}/{runtime.said}/response/{self._client_id}",
                    f"cmd/{model}/{runtime.said}/response/#",
                    f"dt/{model}/{runtime.said}/state/update",
                    f"dt/{model}/{runtime.said}/state/#",
                    f"api/capability/download/{model}/{runtime.said}/response",
                    f"dt/{model}/{runtime.said}/ota/status",
                ]
            )

        seen: set[str] = set()
        loop = asyncio.get_running_loop()
        for topic in topics:
            if topic in seen:
                continue
            seen.add(topic)
            sub_future, _ = self._connection.subscribe(
                topic=topic,
                qos=mqtt.QoS.AT_LEAST_ONCE,
                callback=self._make_callback(topic),
            )
            await loop.run_in_executor(None, lambda f=sub_future: f.result(timeout=CONNECTION_TIMEOUT))
            _LOGGER.debug("Subscribed to Whirlpool MQTT topic %s", topic)

    async def publish_command(
        self,
        runtime: ThingRuntime,
        command: str,
        payload: Mapping[str, Any] | None = None,
        *,
        addressee: str = "appliance",
    ) -> dict[str, Any]:
        """Publish a generic ThingShield command and return the exact payload sent."""
        from awscrt import mqtt

        if self._connection is None or not self._connected:
            raise WhirlpoolConnectionError("MQTT is not connected")
        command_payload = dict(payload or {})
        command_payload.setdefault("addressee", addressee)
        command_payload.setdefault("command", command)
        message = {
            "requestId": str(uuid.uuid4()),
            "timestamp": int(time.time() * 1000),
            "payload": command_payload,
        }
        topic = f"cmd/{runtime.model}/{runtime.said}/request/{self._client_id}"
        loop = asyncio.get_running_loop()
        pub_future, _ = self._connection.publish(
            topic=topic,
            payload=json.dumps(message),
            qos=mqtt.QoS.AT_LEAST_ONCE,
        )
        await loop.run_in_executor(None, lambda: pub_future.result(timeout=CONNECTION_TIMEOUT))
        _LOGGER.debug("Published Whirlpool MQTT command %s to %s", command, topic)
        return {"topic": topic, "payload": message}

    def _make_callback(self, subscribed_topic: str) -> Callable[..., None]:
        def callback(topic: str, payload: bytes, **_: Any) -> None:
            try:
                data = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
                _LOGGER.warning("Could not decode Whirlpool MQTT payload on %s", topic)
                return
            self._on_message(topic, data)

        return callback

    def _on_connection_interrupted(self, connection: Any, error: Exception, **_: Any) -> None:
        _LOGGER.warning("Whirlpool MQTT connection interrupted: %s", error)
        self._connected = False

    def _on_connection_resumed(self, connection: Any, return_code: Any, session_present: bool, **_: Any) -> None:
        _LOGGER.info("Whirlpool MQTT connection resumed: rc=%s session=%s", return_code, session_present)
        self._connected = bool(session_present)


class WhirlpoolThingShieldManager:
    """High-level manager for all TS_SAID appliances on one account."""

    def __init__(self, client: WhirlpoolCloudClient, state_callback: StateCallback) -> None:
        self.client = client
        self._state_callback = state_callback
        self._mqtt = WhirlpoolThingShieldMqttClient(client, self._handle_message)
        self._runtimes: dict[str, ThingRuntime] = {}
        self._online: dict[str, bool] = {}
        self._state: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    @property
    def states(self) -> dict[str, dict[str, Any]]:
        return self._state

    @property
    def runtimes(self) -> dict[str, ThingRuntime]:
        return self._runtimes

    def appliance_online(self, said: str) -> bool | None:
        return self._online.get(said)

    async def ensure_started(self, saids: list[str], appliances: list[Mapping[str, Any]] | None = None) -> None:
        """Connect MQTT and subscribe to every TS_SAID currently known."""
        saids = [s for s in saids if s]
        if not saids:
            return
        model_hints = self._model_hints_by_said(appliances or [])
        async with self._lock:
            if not self._mqtt.connected:
                await self._mqtt.connect()
            for said in saids:
                if said in self._runtimes:
                    continue
                info: ThingInfo | None = None
                model = "Unknown"
                try:
                    info = await self.client.describe_thing(said)
                    model = info.model or "Unknown"
                except WhirlpoolApiError as err:
                    _LOGGER.warning("Could not describe Whirlpool ThingShield appliance %s: %s", said, err)
                if model == "Unknown":
                    # Without the thing type/model, MQTT topics cannot be constructed.
                    self._state[said] = {"error": "missing_thing_type", "detail": "Could not discover AWS IoT thingTypeName/model"}
                    self._state_callback(said, self._state[said])
                    continue
                runtime = ThingRuntime(said=said, model=model, info=info, model_hints=tuple(model_hints.get(said, ())))
                await self._mqtt.subscribe_appliance(runtime)
                self._runtimes[said] = runtime
                self._online.setdefault(said, True)
                await self.request_state(said)

    async def ensure_connected(self) -> None:
        if self._runtimes and not self._mqtt.connected:
            await self._mqtt.disconnect()
            await self._mqtt.connect()
            runtimes = list(self._runtimes.values())
            self._runtimes = {}
            for runtime in runtimes:
                await self._mqtt.subscribe_appliance(runtime)
                self._runtimes[runtime.said] = runtime
                await self.request_state(runtime.said)

    async def request_state(self, said: str) -> dict[str, Any]:
        runtime = self._runtimes.get(said)
        if runtime is None:
            raise WhirlpoolConnectionError(f"ThingShield appliance {said} is not subscribed")
        result = await self._mqtt.publish_command(runtime, "getState")
        # Several Android classes refer to publishGetStatus. It is safe as a
        # read-only follow-up and improves compatibility with non-laundry TS_SAID
        # devices whose firmware ignores getState.
        try:
            await self._mqtt.publish_command(runtime, "getStatus")
        except WhirlpoolApiError as err:
            _LOGGER.debug("Optional ThingShield getStatus request failed for %s: %s", said, err)
        for optional_command in ("getApplianceInfo", "getCapabilities"):
            try:
                await self._mqtt.publish_command(runtime, optional_command)
            except WhirlpoolApiError as err:
                _LOGGER.debug("Optional ThingShield %s request failed for %s: %s", optional_command, said, err)
        return result

    async def publish_command(
        self,
        said: str,
        command: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime = self._runtimes.get(said)
        if runtime is None:
            raise WhirlpoolConnectionError(f"ThingShield appliance {said} is not subscribed")
        return await self._mqtt.publish_command(runtime, command, payload)

    @staticmethod
    def _model_hints_by_said(appliances: list[Mapping[str, Any]]) -> dict[str, list[str]]:
        """Collect possible MQTT model/topic segments from REST appliance metadata."""
        out: dict[str, list[str]] = {}
        for appliance in appliances:
            said = None
            for key in ("said", "SAID", "serialNumber", "applianceSAID", "applianceId", "id"):
                raw = appliance.get(key)
                if raw:
                    said = str(raw)
                    break
            if not said:
                continue
            hints = out.setdefault(said, [])
            for key in (
                "model",
                "modelNumber",
                "MODEL_NO",
                "thingTypeName",
                "DATA_MODEL",
                "DATA_MODEL_KEY",
                "dataModel",
            ):
                raw = appliance.get(key)
                if raw and str(raw) not in hints:
                    hints.append(str(raw))
        return out

    async def shutdown(self) -> None:
        await self._mqtt.disconnect()

    def _handle_message(self, topic: str, data: dict[str, Any]) -> None:
        said = self._said_from_topic(topic)
        if not said:
            _LOGGER.debug("Ignoring Whirlpool MQTT message without SAID in topic %s", topic)
            return
        if "$aws/events/presence/connected/" in topic:
            self._online[said] = True
            state = {**self._state.get(said, {}), "online": True}
            self._state[said] = state
            self._state_callback(said, state)
            return
        if "$aws/events/presence/disconnected/" in topic:
            self._online[said] = False
            state = {**self._state.get(said, {}), "online": False}
            self._state[said] = state
            self._state_callback(said, state)
            return

        topic_model = self._model_from_topic(topic)
        runtime = self._runtimes.get(said)
        if runtime is not None and topic_model and topic_model != "+" and runtime.model != topic_model:
            self._runtimes[said] = ThingRuntime(
                said=runtime.said,
                model=topic_model,
                info=runtime.info,
                model_hints=runtime.model_hints,
            )

        payload = self._unwrap_payload(data)
        if not isinstance(payload, dict):
            payload = {"payload": payload}
        # Command responses often wrap state in payload; state/update topics may already be state.
        state = dict(payload)
        state.setdefault("online", True)
        state.setdefault("mqttTopic", topic)
        state.setdefault("mqttRaw", data)
        if topic_model:
            state.setdefault("topicModel", topic_model)
        if "response" in data:
            state.setdefault("lastResponse", data.get("response"))
        if "requestId" in data:
            state.setdefault("lastRequestId", data.get("requestId"))
        self._online[said] = True
        self._state[said] = state
        self._state_callback(said, state)

    @staticmethod
    def _unwrap_payload(data: dict[str, Any]) -> Any:
        """Return the useful state payload from common MQTT envelope shapes."""
        payload: Any = data.get("payload", data)
        # Some MQTT SDK paths deliver a JSON document as a string inside payload.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return payload
        # A few command envelopes nest the real state one level deeper.
        if isinstance(payload, Mapping):
            for key in (
                "state",
                "data",
                "appliance",
                "attributes",
                "attributeMap",
                "washer",
                "dryer",
                "dishwasher",
                "refrigerator",
                "freezer",
                "airConditioner",
                "aircon",
                "cooktop",
            ):
                nested = payload.get(key)
                if isinstance(nested, (Mapping, list)):
                    # Preserve the outer keys too, but expose nested fields at top
                    # level so generic sensors can find them.
                    if isinstance(nested, Mapping):
                        merged = dict(payload)
                        merged.update(nested)
                        return merged
                    return nested
        return payload

    @staticmethod
    def _model_from_topic(topic: str) -> str | None:
        parts = topic.split("/")
        # cmd/{model}/{said}/..., dt/{model}/{said}/..., api/capability/download/{model}/{said}/...
        if len(parts) >= 3 and parts[0] in {"cmd", "dt"}:
            return parts[1]
        if len(parts) >= 5 and parts[:3] == ["api", "capability", "download"]:
            return parts[3]
        return None

    @staticmethod
    def _said_from_topic(topic: str) -> str | None:
        parts = topic.split("/")
        if "$aws/events/presence" in topic and len(parts) >= 5:
            return parts[-1]
        # cmd/{model}/{said}/response/{clientId}, dt/{model}/{said}/state/update, etc.
        if len(parts) >= 3 and parts[0] in {"cmd", "dt", "api"}:
            if parts[0] == "api" and len(parts) >= 5:
                return parts[4]
            return parts[2]
        return None
