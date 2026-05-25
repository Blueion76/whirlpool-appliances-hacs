"""Async Whirlpool cloud client based on API surfaces observed in the APK."""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import (
    AWS_REGIONS,
    BASE_URLS,
    CLIENT_CREDENTIALS,
    DEFAULT_BRAND,
    DEFAULT_REGION,
    IOT_ENDPOINTS,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_KEYS = ("access_token", "accessToken", "id_token", "idToken", "token", "memberToken")
REFRESH_KEYS = ("refresh_token", "refreshToken")
ACCOUNT_KEYS = ("accountId", "account_id", "id", "userId", "user_id")
SAID_KEYS = ("said", "SAID", "serialNumber", "serial_number", "applianceSAID", "applianceId", "id")


class WhirlpoolApiError(Exception):
    """Base Whirlpool API error."""


class WhirlpoolAuthError(WhirlpoolApiError):
    """Authentication failed."""


class WhirlpoolAccountLockedError(WhirlpoolAuthError):
    """Whirlpool account is locked."""


class WhirlpoolConnectionError(WhirlpoolApiError):
    """Realtime connection failed."""


@dataclass(slots=True)
class AwsCredentials:
    """Temporary AWS credentials from Cognito."""

    access_key: str
    secret_key: str
    session_token: str
    expiration: float


@dataclass(slots=True)
class ThingInfo:
    """AWS IoT thing metadata."""

    said: str
    model: str
    brand: str
    category: str
    serial: str
    name: str
    thing_id: str


class WhirlpoolCloudClient:
    """Resilient client for Whirlpool's mobile REST API plus Cognito hand-off.

    The Android app exposes two appliance generations:
    * SAID / legacy appliances: REST API + STOMP/WebSocket style status/commands.
    * TS_SAID / ThingShield appliances: OAuth + Cognito + AWS IoT MQTT.

    This client owns OAuth and REST; :mod:`api_mqtt` uses the Cognito helpers here.
    """

    def __init__(
        self,
        session: ClientSession,
        username: str,
        password: str,
        *,
        region: str = DEFAULT_REGION,
        brand: str = DEFAULT_BRAND,
        environment: str = "prod",
        base_url: str | None = None,
    ) -> None:
        self.session = session
        self.username = username
        self.password = password
        self.region = region.upper()
        self.brand = brand
        self.environment = environment
        self.base_url = (
            base_url
            or BASE_URLS.get((self.region, environment), BASE_URLS[(DEFAULT_REGION, "prod")])
        ).rstrip("/")
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.account_id: str | None = None
        self.user_id: str | None = None
        self._active_client_id: str | None = None
        self._active_client_secret: str | None = None
        self.token_expiry: float = 0
        self.auth_payload: dict[str, Any] = {}
        self._login_lock = asyncio.Lock()
        self._aws_credentials: AwsCredentials | None = None
        self._aws_credentials_identity_id: str | None = None
        self._aws_credentials_expiry_skew = 300

    @property
    def authenticated(self) -> bool:
        """Return whether an access token is present."""
        return bool(self.access_token)

    @property
    def client_id(self) -> str:
        return self._active_client_id or self._client_credentials()[0]

    @property
    def client_secret(self) -> str:
        return self._active_client_secret or self._client_credentials()[1]

    @property
    def aws_region(self) -> str:
        return AWS_REGIONS.get(self.region, AWS_REGIONS[DEFAULT_REGION])

    @property
    def iot_endpoint(self) -> str:
        return IOT_ENDPOINTS.get(self.region, IOT_ENDPOINTS[DEFAULT_REGION])

    def _client_credentials(self) -> tuple[str, str]:
        return CLIENT_CREDENTIALS.get(
            (self.region, self.brand),
            CLIENT_CREDENTIALS.get((self.region, DEFAULT_BRAND), CLIENT_CREDENTIALS[(DEFAULT_REGION, DEFAULT_BRAND)]),
        )

    def _credential_candidates(self) -> list[tuple[str, str, str]]:
        """Return OAuth mobile app credentials to try, selected brand first.

        MizterB/homeassistant-whirlpool and whirlpool-sixth-sense authenticate only
        through /oauth/token. This keeps that behavior, but makes account setup more
        forgiving when a user chooses the wrong brand by trying the other public
        mobile clients in the same region after the selected brand fails.
        """
        candidates: list[tuple[str, str, str]] = []

        def add(label: str, creds: tuple[str, str] | None) -> None:
            if not creds:
                return
            item = (label, creds[0], creds[1])
            if item not in candidates:
                candidates.append(item)

        add(self.brand, CLIENT_CREDENTIALS.get((self.region, self.brand)))
        add(DEFAULT_BRAND, CLIENT_CREDENTIALS.get((self.region, DEFAULT_BRAND)))
        for (region, brand), creds in CLIENT_CREDENTIALS.items():
            if region == self.region:
                add(brand, creds)
        if not candidates:
            add(DEFAULT_BRAND, CLIENT_CREDENTIALS[(DEFAULT_REGION, DEFAULT_BRAND)])
        return candidates

    async def _oauth_token_request(
        self,
        data: Mapping[str, Any],
        *,
        client_label: str,
        client_id: str,
        client_secret: str,
    ) -> Any:
        """Post to /oauth/token using the same minimal headers as whirlpool-sixth-sense."""
        url = f"{self.base_url}/oauth/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        }
        try:
            async with self.session.post(url, data=data, headers=headers, timeout=30) as resp:
                text = await resp.text()
                if resp.status == 423:
                    raise WhirlpoolAccountLockedError("Whirlpool account is locked")
                if resp.status != 200:
                    raise WhirlpoolAuthError(
                        f"{client_label} OAuth failed with HTTP {resp.status}: {text[:500]}"
                    )
                try:
                    payload = await resp.json(content_type=None)
                except Exception as err:  # noqa: BLE001
                    raise WhirlpoolAuthError(
                        f"{client_label} OAuth returned non-JSON response: {text[:500]}"
                    ) from err
                self._active_client_id = client_id
                self._active_client_secret = client_secret
                return payload
        except WhirlpoolAuthError:
            raise
        except ClientError as err:
            raise WhirlpoolApiError(f"Connection error during OAuth: {err}") from err

    async def login(self) -> None:
        """Authenticate using the Whirlpool mobile OAuth flow used by MizterB."""
        async with self._login_lock:
            if self.access_token and time.time() < self.token_expiry - 60:
                return
            self.access_token = None
            errors: list[str] = []
            for client_label, client_id, client_secret in self._credential_candidates():
                auth_data = {
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                    "client_id": client_id,
                    "client_secret": client_secret,
                }
                try:
                    result = await self._oauth_token_request(
                        auth_data,
                        client_label=client_label,
                        client_id=client_id,
                        client_secret=client_secret,
                    )
                except WhirlpoolAccountLockedError:
                    raise
                except WhirlpoolAuthError as err:
                    errors.append(str(err))
                    continue
                self._extract_session(result)
                if self.access_token:
                    await self._populate_user_details()
                    return
                errors.append(f"{client_label} OAuth succeeded but no access_token was returned")
            self.auth_payload = {}
            self.refresh_token = None
            self.account_id = None
            self.user_id = None
            detail = "; ".join(errors[-3:])
            raise WhirlpoolAuthError(f"Authentication failed through /oauth/token. {detail}")

    async def refresh_auth(self) -> None:
        """Refresh the OAuth token, falling back to username/password if needed."""
        if not self.refresh_token:
            self.access_token = None
            await self.login()
            return
        try:
            result = await self._oauth_token_request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                client_label=self.brand,
                client_id=self.client_id,
                client_secret=self.client_secret,
            )
            self._extract_session(result)
        except WhirlpoolAccountLockedError:
            raise
        except WhirlpoolAuthError:
            self.access_token = None
            await self.login()

    async def ensure_auth_valid(self) -> None:
        """Refresh auth if token expiry is close."""
        if not self.access_token:
            await self.login()
            return
        if self.token_expiry and time.time() >= self.token_expiry - 300:
            await self.refresh_auth()

    async def _populate_user_details(self) -> None:
        """Best-effort account profile lookup without invalidating a fresh token."""
        token_before = self.access_token
        for path in ("/api/v1/getUserDetails", "/api/v1/sso/profiles"):
            try:
                details = await self.request("GET", path)
            except WhirlpoolApiError:
                # Some regions/accounts do not allow every profile endpoint. This
                # lookup is optional; keep the OAuth token obtained from /oauth/token.
                if token_before and not self.access_token:
                    self.access_token = token_before
                continue
            self._extract_session(details)
            if self.account_id or self.user_id:
                return

    def _headers(self, auth: bool = True, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Pragma": "no-cache",
            "Cache-Control": "no-cache",
            "x-client-brand": self.brand,
            "x-client-region": self.region,
            "WP-CLIENT-BRAND": self.brand,
        }
        if auth and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if extra:
            headers.update(extra)
        return headers

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        data: Any | None = None,
        params: Mapping[str, Any] | None = None,
        auth: bool = True,
        extra_headers: Mapping[str, str] | None = None,
    ) -> Any:
        """Call an API path and return decoded JSON or text."""
        if auth:
            await self.ensure_auth_valid()
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        headers = self._headers(auth=auth, extra=extra_headers)
        try:
            async with self.session.request(
                method.upper(),
                url,
                json=json,
                data=data,
                params=params,
                headers=headers,
                timeout=30,
            ) as resp:
                return await self._decode_response(resp)
        except ClientError as err:
            raise WhirlpoolApiError(str(err)) from err

    async def _decode_response(self, resp: ClientResponse) -> Any:
        text = await resp.text()
        if resp.status in (401, 403):
            # Some Whirlpool endpoints legitimately reject certain appliance generations
            # (for example legacy REST calls against TS_SAID devices). Do not erase a
            # valid OAuth token here; callers decide whether the failed endpoint was
            # required or optional.
            raise WhirlpoolAuthError(f"HTTP {resp.status}: {text[:500]}")
        if resp.status >= 400:
            raise WhirlpoolApiError(f"HTTP {resp.status}: {text[:1000]}")
        content_type = resp.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            try:
                return await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                pass
        if not text:
            return None
        try:
            return jsonlib.loads(text)
        except Exception:  # noqa: BLE001
            return text

    def _extract_session(self, payload: Any) -> None:
        """Recursively extract token/account/appliance fields from variable payloads."""
        if isinstance(payload, Mapping):
            # Keep the top-level auth payload because OAuth returns SAID/TS_SAID arrays there.
            if any(key in payload for key in ("access_token", "accessToken", "TS_SAID", "SAID")):
                self.auth_payload = {**self.auth_payload, **dict(payload)}
            for key, value in payload.items():
                if value is None:
                    continue
                if key in TOKEN_KEYS and isinstance(value, str) and value:
                    self.access_token = value
                elif key in REFRESH_KEYS and isinstance(value, str) and value:
                    self.refresh_token = value
                elif key in ("expires_in", "expiresIn") and isinstance(value, (str, int, float)):
                    try:
                        self.token_expiry = time.time() + float(value)
                    except ValueError:
                        pass
                elif key in ("accountId", "account_id") and isinstance(value, (str, int)):
                    self.account_id = str(value)
                elif key in ("userId", "user_id") and isinstance(value, (str, int)):
                    self.user_id = str(value)
                if isinstance(value, (Mapping, list)):
                    self._extract_session(value)
        elif isinstance(payload, list):
            for item in payload:
                self._extract_session(item)

    def _auth_list(self, key: str) -> list[str]:
        raw = self.auth_payload.get(key)
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [str(item) for item in raw if item]
        return []

    def thing_shield_saids(self) -> list[str]:
        """Return TS_SAID appliance IDs from the auth response."""
        return self._auth_list("TS_SAID")

    def legacy_saids(self) -> list[str]:
        """Return legacy SAID appliance IDs from the auth response."""
        return self._auth_list("SAID")

    async def get_user_details(self) -> Any:
        return await self.request("GET", "/api/v1/getUserDetails")

    async def list_locations(self) -> list[dict[str, Any]]:
        account_id = self.account_id
        if not account_id:
            await self._populate_user_details()
            account_id = self.account_id
        if not account_id:
            return []
        result = await self.request("GET", f"/api/v1/location/allLocations/account/{account_id}")
        return _coerce_list(result, keys=("locations", "data", "items"))

    async def list_appliances(self) -> list[dict[str, Any]]:
        """Return all known appliances from REST discovery plus OAuth SAID arrays."""
        await self._populate_user_details()
        appliances: dict[str, dict[str, Any]] = {}
        account_id = self.account_id
        if account_id:
            for path in (
                f"/api/v3/appliance/all/account/{account_id}",
                f"/adrs/api/v2/registrations/accounts/{account_id}",
            ):
                try:
                    for item in _coerce_list(
                        await self.request("GET", path),
                        keys=("appliances", "registrations", "data", "items", "legacyAppliance", "tsAppliance"),
                    ):
                        key = appliance_said(item) or appliance_id(item) or repr(item)
                        appliances[key] = {**appliances.get(key, {}), **item}
                except WhirlpoolApiError as err:
                    _LOGGER.debug("Appliance listing endpoint failed: %s", err)
        for loc in await self.list_locations():
            loc_id = _first_value(loc, ("locationId", "id"))
            if not loc_id:
                continue
            try:
                for item in _coerce_list(
                    await self.request("GET", f"/api/v1/appliancesForLoc/{loc_id}"),
                    keys=("appliances", "data", "items", "legacyAppliance", "tsAppliance"),
                ):
                    key = appliance_said(item) or appliance_id(item) or repr(item)
                    appliances[key] = {**appliances.get(key, {}), **item, "locationId": loc_id}
            except WhirlpoolApiError as err:
                _LOGGER.debug("Location appliance endpoint failed for %s: %s", loc_id, err)

        for said in self.legacy_saids():
            appliances.setdefault(
                said,
                {"said": said, "SAID": said, "name": f"Whirlpool appliance {said}", "source": "SAID", "thingShield": False},
            )
        for said in self.thing_shield_saids():
            appliances.setdefault(
                said,
                {
                    "said": said,
                    "SAID": said,
                    "name": f"Whirlpool ThingShield {said}",
                    "source": "TS_SAID",
                    "thingShield": True,
                    "category": "ThingShield",
                },
            )
        return list(appliances.values())

    async def get_status(self, said: str) -> Any:
        return await self.request("GET", f"/api/v1/appliance/status/{said}")

    async def get_appliance(self, said: str) -> Any:
        return await self.request("GET", f"/api/v1/appliance/{said}")

    async def sync_appliance(self, said: str) -> Any:
        return await self.request("GET", f"/api/v1/appliance/{said}/sync")

    async def get_cognito_identity(self) -> tuple[str, str]:
        """Return (identity_id, cognito_token) for ThingShield AWS IoT."""
        await self.ensure_auth_valid()
        result = await self.request("GET", "/api/v1/cognito/identityid")
        if not isinstance(result, Mapping):
            raise WhirlpoolApiError(f"Unexpected Cognito identity response: {result!r}")
        identity_id = result.get("identityId") or result.get("IdentityId")
        token = result.get("token") or result.get("Token")
        if not isinstance(identity_id, str) or not isinstance(token, str):
            raise WhirlpoolApiError(f"Missing Cognito identity/token in response: {result!r}")
        return identity_id, token

    async def get_aws_credentials(self, *, force: bool = False) -> tuple[str, AwsCredentials]:
        """Exchange Cognito identity/token for temporary AWS credentials."""
        now = time.time()
        if (
            not force
            and self._aws_credentials is not None
            and self._aws_credentials_identity_id
            and now < self._aws_credentials.expiration - self._aws_credentials_expiry_skew
        ):
            return self._aws_credentials_identity_id, self._aws_credentials

        identity_id, cognito_token = await self.get_cognito_identity()
        loop = asyncio.get_running_loop()

        def _get_credentials() -> AwsCredentials:
            import boto3

            cognito = boto3.client(
                "cognito-identity",
                region_name=self.aws_region,
                aws_access_key_id="anonymous",
                aws_secret_access_key="anonymous",
            )
            response = cognito.get_credentials_for_identity(
                IdentityId=identity_id,
                Logins={"cognito-identity.amazonaws.com": cognito_token},
            )
            creds = response["Credentials"]
            return AwsCredentials(
                access_key=creds["AccessKeyId"],
                secret_key=creds["SecretKey"],
                session_token=creds["SessionToken"],
                expiration=creds["Expiration"].timestamp(),
            )

        try:
            self._aws_credentials = await loop.run_in_executor(None, _get_credentials)
            self._aws_credentials_identity_id = identity_id
        except Exception as err:  # noqa: BLE001
            raise WhirlpoolApiError(f"Failed to obtain AWS IoT credentials: {err}") from err
        return identity_id, self._aws_credentials

    async def describe_thing(self, said: str) -> ThingInfo:
        """Describe a ThingShield appliance via AWS IoT DescribeThing."""
        _, aws_creds = await self.get_aws_credentials()
        loop = asyncio.get_running_loop()

        def _describe() -> ThingInfo:
            import boto3

            iot = boto3.client(
                "iot",
                region_name=self.aws_region,
                aws_access_key_id=aws_creds.access_key,
                aws_secret_access_key=aws_creds.secret_key,
                aws_session_token=aws_creds.session_token,
            )
            desc = iot.describe_thing(thingName=said)
            attrs = desc.get("attributes", {}) or {}
            raw_name = attrs.get("Name", "")
            try:
                name = bytes.fromhex(raw_name).decode() if raw_name else said
            except Exception:
                name = raw_name or said
            return ThingInfo(
                said=said,
                model=desc.get("thingTypeName", "Unknown"),
                brand=attrs.get("Brand", self.brand),
                category=attrs.get("Category", ""),
                serial=attrs.get("Serial", ""),
                name=name,
                thing_id=desc.get("thingId", ""),
            )

        try:
            return await loop.run_in_executor(None, _describe)
        except Exception as err:  # noqa: BLE001
            raise WhirlpoolApiError(f"Failed to describe ThingShield appliance {said}: {err}") from err

    async def call_function(
        self,
        function: str,
        *,
        said: str | None = None,
        body: Any | None = None,
        **path_values: Any,
    ) -> Any:
        from .api_spec import APPLIANCE_FUNCTIONS

        spec = APPLIANCE_FUNCTIONS[function]
        path = spec["path"]
        if said:
            path_values.setdefault("said", said)
        for key, value in path_values.items():
            path = path.replace("{" + key + "}", str(value))
        return await self.request(spec["method"], path, json=body)

    async def send_appliance_command(
        self,
        said: str,
        command: str,
        attributes: Mapping[str, Any] | None = None,
        raw: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send a Whirlpool appliance command through /api/v1/appliance/command.

        Legacy SAID appliances, including Minerva ovens, expect the same shape
        used by whirlpool-sixth-sense:
        {"header": {"said": SAID, "command": "setAttributes"}, "body": {...}}.
        Raw mode is left available for experimenting with APK-discovered command
        envelopes.
        """
        if raw:
            payload = dict(raw)
            payload.setdefault("header", {}).setdefault("said", said)
        else:
            payload = {
                "header": {"said": said, "command": command or "setAttributes"},
                "body": dict(attributes or {}),
            }
        return await self.request("POST", "/api/v1/appliance/command", json=payload)

    async def send_attributes(self, said: str, attributes: Mapping[str, Any]) -> Any:
        """Set legacy SAID appliance attributes."""
        return await self.send_appliance_command(
            said, "setAttributes", {key: str(value) for key, value in attributes.items()}
        )

    @staticmethod
    def _cavity_prefix(cavity: str | None = None) -> str:
        if cavity and str(cavity).lower().startswith("lower"):
            return "OvenLowerCavity"
        return "OvenUpperCavity"

    async def set_cavity_light(self, said: str, on: bool, cavity: str | None = None) -> Any:
        if cavity and str(cavity).lower().startswith(("mwo", "micro")):
            return await self.send_attributes(said, {"Mwo_DisplaySetLightOn": "1" if on else "0"})
        prefix = self._cavity_prefix(cavity)
        return await self.send_attributes(
            said, {f"{prefix}_DisplaySetLightOn": "1" if on else "0"}
        )

    async def set_power(self, said: str, on: bool) -> Any:
        if on:
            # Ovens cannot be generically powered on without a cooking mode and
            # target temperature. Use set_oven_cook for that.
            raise WhirlpoolApiError("Oven power-on requires a mode and target temperature; use set_oven_cook")
        return await self.stop_oven_cavity(said, "upper")

    async def set_target_temperature(self, said: str, temperature: float, cavity: str | None = None) -> Any:
        prefix = self._cavity_prefix(cavity)
        return await self.send_attributes(
            said, {f"{prefix}_CycleSetTargetTemp": str(round(float(temperature) * 10))}
        )

    async def set_oven_cook(
        self, said: str, temperature: float, mode: str = "bake", cavity: str | None = None
    ) -> Any:
        """Start/modify a Whirlpool legacy oven cavity cook cycle."""
        prefix = self._cavity_prefix(cavity)
        mode_map = {
            "standby": "0",
            "bake": "2",
            "convect_bake": "6",
            "convection_bake": "6",
            "broil": "8",
            "convect_broil": "9",
            "convection_broil": "9",
            "convect_roast": "16",
            "convection_roast": "16",
            "keep_warm": "24",
            "air_fry": "41",
        }
        selected = mode_map.get(str(mode).lower())
        if selected is None:
            raise WhirlpoolApiError(f"Unsupported oven cook mode: {mode}")
        return await self.send_attributes(
            said,
            {
                f"{prefix}_CycleSetCommonMode": selected,
                f"{prefix}_CycleSetTargetTemp": str(round(float(temperature) * 10)),
                f"{prefix}_OpSetOperations": "2",
            },
        )

    async def stop_oven_cavity(self, said: str, cavity: str | None = None) -> Any:
        prefix = self._cavity_prefix(cavity)
        return await self.send_attributes(said, {f"{prefix}_OpSetOperations": "1"})

    async def stop_microwave(self, said: str) -> Any:
        return await self.send_attributes(said, {"Mwo_OperationSetOperations": "1"})

    @staticmethod
    def _attr_from_appliance_payload(payload: Any, attr: str) -> Any | None:
        """Read a legacy Whirlpool attribute from an appliance payload."""
        if isinstance(payload, Mapping):
            attrs = payload.get("attributes")
            if isinstance(attrs, Mapping):
                item = attrs.get(attr)
                if isinstance(item, Mapping):
                    return item.get("value")
                if item is not None:
                    return item
            for value in payload.values():
                found = WhirlpoolCloudClient._attr_from_appliance_payload(value, attr)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for item in payload:
                found = WhirlpoolCloudClient._attr_from_appliance_payload(item, attr)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _format_appliance_datetime(value: datetime) -> str:
        """Format datetime like the Whirlpool Android app legacy payload.

        Minerva cooking appliances expose XCat_DateTimeSetDateTimeSet values in
        a non-standard but consistent format, for example
        ``2026- 3-14T 8:26:00+00:00``. The APK's DateTimeSet DTO maps to this
        attribute, so keep Whirlpool's spacing instead of using strict ISO-8601.
        """
        utc_value = value.astimezone(timezone.utc)
        return (
            f"{utc_value.year:04d}-{utc_value.month:2d}-{utc_value.day:02d}"
            f"T{utc_value.hour:2d}:{utc_value.minute:02d}:{utc_value.second:02d}+00:00"
        )

    async def sync_appliance_time(self, said: str, timezone_name: str | None = None) -> Any:
        """Sync current time/timezone to a legacy Whirlpool appliance.

        The Android APK contains a DateTimeSet flow/DTO. On the Minerva
        oven/microwave payload this maps to ``XCat_DateTimeSetDateTimeSet`` plus
        timezone/offset attributes. We fetch the appliance first so the button
        uses the appliance's configured IANA timezone when available, then set
        the current UTC timestamp and local offset values through the same
        legacy setAttributes command path used by the app.
        """
        status = await self.get_appliance(said)
        detected_tz = timezone_name or self._attr_from_appliance_payload(status, "TimeZoneId")
        detected_tz = detected_tz or self._attr_from_appliance_payload(status, "TimezoneId")
        detected_tz = detected_tz or "UTC"
        try:
            tzinfo = ZoneInfo(str(detected_tz))
        except ZoneInfoNotFoundError:
            _LOGGER.warning("Unknown appliance timezone %r for %s; falling back to UTC", detected_tz, said)
            detected_tz = "UTC"
            tzinfo = ZoneInfo("UTC")

        now_local = datetime.now(tzinfo).replace(microsecond=0)
        now_utc = now_local.astimezone(timezone.utc)
        offset = now_local.utcoffset()
        dst = now_local.dst()
        offset_seconds = int(offset.total_seconds()) if offset is not None else 0
        dst_seconds = int(dst.total_seconds()) if dst is not None else 0
        attrs = {
            "XCat_DateTimeSetDateTimeSet": self._format_appliance_datetime(now_utc),
            "DateTimeMode": "2",
            "XCat_DateTimeMode": "2",
            "TimeZoneId": str(detected_tz),
            "TimezoneId": str(detected_tz),
            "UtcOffset": str(offset_seconds),
            "XCat_UtcOffset": str(offset_seconds),
            "DstOffset": str(dst_seconds),
            "XCat_DstOffset": str(dst_seconds),
        }
        return await self.send_attributes(said, attrs)

    async def set_microwave_turntable(self, said: str, enabled: bool) -> Any:
        return await self.send_attributes(said, {"Mwo_CycleSetTurntable": "1" if enabled else "0"})

    async def set_oven_control_lock(self, said: str, locked: bool) -> Any:
        return await self.send_attributes(said, {"Sys_OperationSetControlLock": "1" if locked else "0"})

    async def set_oven_sabbath_mode(self, said: str, enabled: bool) -> Any:
        return await self.send_attributes(said, {"Sys_OperationSetSabbathModeEnabled": "1" if enabled else "0"})


def _first_value(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any | None:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _coerce_list(payload: Any, *, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    """Extract all dict-like appliances from nested Whirlpool responses."""
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    if appliance_said(item) or appliance_id(item) or any(k in item for k in ("DATA_MODEL", "CATEGORY")):
                        found.append(dict(item))
                    walk(item)
        elif isinstance(value, Mapping):
            for key in keys:
                item = value.get(key)
                if item is not None:
                    walk(item)
            if appliance_said(value) or appliance_id(value):
                found.append(dict(value))
            # Account/location endpoint nests account -> location -> legacyAppliance/tsAppliance.
            for item in value.values():
                if isinstance(item, (Mapping, list)):
                    walk(item)

    walk(payload)
    unique: dict[str, dict[str, Any]] = {}
    for item in found:
        key = appliance_said(item) or appliance_id(item) or repr(sorted(item.items()))
        unique[key] = {**unique.get(key, {}), **item}
    return list(unique.values())


def appliance_said(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(appliance, SAID_KEYS)
    return str(value) if value is not None else None


def appliance_id(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(appliance, ("applianceId", "id", "serialNumber", "said", "SAID"))
    return str(value) if value is not None else None


def appliance_name(appliance: Mapping[str, Any]) -> str:
    value = _first_value(
        appliance,
        (
            "APPLIANCE_NAME",
            "applianceName",
            "name",
            "Name",
            "nickname",
            "CATEGORY_NAME",
            "category",
            "CATEGORY",
            "MODEL_NO",
            "modelNumber",
            "model",
            "DATA_MODEL_KEY",
            "DATA_MODEL",
        ),
    )
    return str(value) if value is not None else "Whirlpool appliance"
