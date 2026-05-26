"""Async Whirlpool cloud client based on API surfaces observed in the APK."""
from __future__ import annotations

import asyncio
import json as jsonlib
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .logging_utils import summarize, summarize_keys

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
DDM_KEYS = (
    "ddmKey",
    "DDM_KEY",
    "dataModelKey",
    "DATA_MODEL_KEY",
    "data_model_key",
    "dataModel",
    "DATA_MODEL",
)


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
        self._ddm_capability_cache: dict[str, Any] = {}

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
        safe_path = path if path.startswith("/") else url.split(self.base_url, 1)[-1] or path
        _LOGGER.debug(
            "Whirlpool API request: method=%s path=%s auth=%s params=%s json=%s data=%s",
            method.upper(),
            safe_path,
            auth,
            summarize(params),
            summarize(json),
            summarize(data),
        )
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
                result = await self._decode_response(resp)
                _LOGGER.debug(
                    "Whirlpool API response: method=%s path=%s status=%s payload=%s",
                    method.upper(),
                    safe_path,
                    resp.status,
                    summarize_keys(result),
                )
                _LOGGER.debug(
                    "Whirlpool API response payload preview: method=%s path=%s payload=%s",
                    method.upper(),
                    safe_path,
                    summarize(result),
                )
                return result
        except ClientError as err:
            _LOGGER.warning("Whirlpool API transport error: method=%s path=%s error=%s", method.upper(), safe_path, err)
            raise WhirlpoolApiError(str(err)) from err

    async def _decode_response(self, resp: ClientResponse) -> Any:
        text = await resp.text()
        if resp.status in (401, 403):
            _LOGGER.warning(
                "Whirlpool API auth/error response: status=%s url=%s body=%s",
                resp.status,
                resp.url.path,
                summarize(text),
            )
            # Some Whirlpool endpoints legitimately reject certain appliance generations
            # (for example legacy REST calls against TS_SAID devices). Do not erase a
            # valid OAuth token here; callers decide whether the failed endpoint was
            # required or optional.
            raise WhirlpoolAuthError(f"HTTP {resp.status}: {text[:500]}")
        if resp.status >= 400:
            _LOGGER.warning(
                "Whirlpool API error response: status=%s url=%s body=%s",
                resp.status,
                resp.url.path,
                summarize(text),
            )
            raise WhirlpoolApiError(f"HTTP {resp.status}: {text[:1000]}")
        content_type = resp.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            try:
                return await resp.json(content_type=None)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Whirlpool API response declared JSON but failed to decode: url=%s", resp.url.path)
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

    async def get_ddm_capabilities(self, ddm_key: str, *, said: str | None = None, force: bool = False) -> Any:
        """Fetch and cache Whirlpool DDM/capability data for a data-model key.

        The Whirlpool app downloads per-model capability data from
        /api/v1/contents/all/{ddmKey}. Cache by DDM key because multiple
        appliances/models can share the same capability file.
        """
        key = str(ddm_key or "").strip()
        if not key:
            raise WhirlpoolApiError("Missing DDM key")
        cache_key = f"{key}:{said or ''}"
        if not force and cache_key in self._ddm_capability_cache:
            return self._ddm_capability_cache[cache_key]

        # The public APK string table contains /api/v1/contents/all/{ddmKey},
        # but live accounts can return API Gateway/S3 errors for that route.
        # The maintained whirlpool_hacs client fetches the data model through
        # POST /api/v2/DeviceDataModel with {"saIdList": [said]}, which is the
        # endpoint that returns the per-appliance DDM/capability payload.
        if said:
            data = await self.request(
                "POST",
                "/api/v2/DeviceDataModel",
                json={"saIdList": [said]},
                extra_headers={
                    "WP-CLIENT-COUNTRY": self.region,
                    "WP-CLIENT-BRAND": self.brand,
                },
            )
            self._ddm_capability_cache[cache_key] = data
            return data

        # Last-resort fallback for future accounts where the content route works.
        data = await self.request("GET", f"/api/v1/contents/all/{key}")
        self._ddm_capability_cache[cache_key] = data
        return data

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
        _LOGGER.debug(
            "Sending Whirlpool appliance command: said=%s command=%s body=%s raw=%s",
            said,
            command or "setAttributes",
            summarize(payload.get("body")),
            bool(raw),
        )
        result = await self.request("POST", "/api/v1/appliance/command", json=payload)
        if self._command_rejected(result):
            _LOGGER.warning(
                "Whirlpool appliance command rejected: said=%s command=%s body=%s result=%s",
                said,
                command or "setAttributes",
                summarize(payload.get("body")),
                summarize(result),
            )
        else:
            _LOGGER.debug(
                "Whirlpool appliance command accepted: said=%s command=%s result=%s",
                said,
                command or "setAttributes",
                summarize_keys(result),
            )
        return result

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

    @staticmethod
    def _command_rejected(result: Any) -> bool:
        """Return true for Whirlpool command negative acknowledgements."""
        if result is False:
            return True
        if isinstance(result, Mapping):
            status = str(result.get("status", "")).strip().lower()
            message = str(result.get("message", "")).strip().lower()
            return status in {"error", "failed", "fail", "02", "2", "nack"} or "negative acknow" in message
        return False

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
        self,
        said: str,
        temperature: float,
        mode: str = "bake",
        cavity: str | None = None,
        *,
        cook_time_seconds: int | None = None,
        delay_time_seconds: int | None = None,
        complete_action: str = "turn_off",
    ) -> Any:
        """Start/modify a Whirlpool legacy oven cavity cook cycle.

        Temperature is Celsius and is converted to Whirlpool tenths-of-Celsius.
        Optional cook time, delay time, and completion action are sourced from
        the DDM CapabilityData when available.
        """
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
        complete_action_map = {
            "stay_on": "1",
            "stayon": "1",
            "keep_warm": "2",
            "keepwarm": "2",
            "turn_off": "3",
            "turnoff": "3",
        }
        selected = mode_map.get(str(mode).lower())
        if selected is None:
            raise WhirlpoolApiError(f"Unsupported oven cook mode: {mode}")

        complete = complete_action_map.get(str(complete_action or "turn_off").lower())
        if complete is None:
            raise WhirlpoolApiError(f"Unsupported oven cook time complete action: {complete_action}")

        attrs = {
            f"{prefix}_CycleSetCommonMode": selected,
            f"{prefix}_CycleSetTargetTemp": str(int(round(float(temperature) * 10))),
            f"{prefix}_OpSetCookTimeCompleteAction": complete,
            f"{prefix}_OpSetOperations": "2",
        }
        _LOGGER.debug(
            "Preparing Whirlpool oven cook command: said=%s cavity=%s mode=%s code=%s target_c=%s cook_time_seconds=%s delay_time_seconds=%s complete_action=%s code=%s",
            said,
            cavity or "upper",
            mode,
            selected,
            temperature,
            cook_time_seconds,
            delay_time_seconds,
            complete_action,
            complete,
        )
        if cook_time_seconds is not None and int(cook_time_seconds) > 0:
            attrs[f"{prefix}_TimeSetCookTimeSet"] = str(int(cook_time_seconds))
        if delay_time_seconds is not None and int(delay_time_seconds) > 0:
            attrs[f"{prefix}_TimeSetDelayTime"] = str(int(delay_time_seconds))

        _LOGGER.debug("Final Whirlpool oven cook attributes: said=%s cavity=%s attrs=%s", said, cavity or "upper", summarize(attrs))
        return await self.send_attributes(said, attrs)

    async def set_oven_frozen_bake(
        self,
        said: str,
        food: str,
        temperature: float,
        cook_time_seconds: int,
        cavity: str | None = None,
        *,
        complete_action: str = "turn_off",
    ) -> Any:
        """Start a Whirlpool Frozen Bake automatic oven cycle."""
        prefix = self._cavity_prefix(cavity)
        food_map = {
            "meals": "2",
            "nuggets": "3",
            "lasagna": "4",
            "pizza": "5",
            "pie": "6",
            "fries": "7",
        }
        complete_action_map = {
            "stay_on": "1",
            "stayon": "1",
            "keep_warm": "2",
            "keepwarm": "2",
            "turn_off": "3",
            "turnoff": "3",
        }
        selected_food = food_map.get(str(food).lower())
        if selected_food is None:
            raise WhirlpoolApiError(f"Unsupported Frozen Bake food: {food}")

        complete = complete_action_map.get(str(complete_action or "turn_off").lower())
        if complete is None:
            raise WhirlpoolApiError(f"Unsupported oven cook time complete action: {complete_action}")

        attrs = {
            f"{prefix}_CycleSetFrozenBakeFood": selected_food,
            f"{prefix}_CycleSetTargetTemp": str(int(round(float(temperature) * 10))),
            f"{prefix}_TimeSetCookTimeSet": str(max(0, int(cook_time_seconds))),
            f"{prefix}_OpSetCookTimeCompleteAction": complete,
            f"{prefix}_OpSetOperations": "2",
        }
        _LOGGER.debug(
            "Preparing Whirlpool Frozen Bake command: said=%s cavity=%s food=%s code=%s target_c=%s cook_time_seconds=%s complete_action=%s code=%s",
            said,
            cavity or "upper",
            food,
            selected_food,
            temperature,
            cook_time_seconds,
            complete_action,
            complete,
        )
        return await self.send_attributes(said, attrs)

    async def stop_oven_cavity(self, said: str, cavity: str | None = None) -> Any:
        prefix = self._cavity_prefix(cavity)
        return await self.send_attributes(said, {f"{prefix}_OpSetOperations": "1"})

    async def stop_microwave(self, said: str) -> Any:
        return await self.send_attributes(said, {"Mwo_OperationSetOperations": "1"})

    async def set_kitchen_timer(self, said: str, seconds: int, timer: int = 1) -> Any:
        """Set/start a Whirlpool on-screen kitchen timer.

        Captured Android app 6.8.3 uses:
          KitchenTimer01_SetTimeSet: seconds
          KitchenTimer01_SetOperations: 2

        ``timer`` is reserved for future multi-timer models; current Minerva
        payloads expose KitchenTimer01.
        """
        seconds = max(0, int(seconds))
        timer_attr = f"KitchenTimer{int(timer):02d}"
        return await self.send_attributes(
            said,
            {
                f"{timer_attr}_SetTimeSet": str(seconds),
                f"{timer_attr}_SetOperations": "2",
            },
        )

    async def stop_kitchen_timer(self, said: str, timer: int = 1) -> Any:
        """Stop/cancel a Whirlpool on-screen kitchen timer.

        The app sends timer commands through the same /api/v1/appliance/command
        setAttributes endpoint and includes the time value alongside the
        operation. Preserve the last known timer duration when cancelling so the
        appliance receives the same command shape as the app.
        """
        timer_attr = f"KitchenTimer{int(timer):02d}"
        seconds = 0
        try:
            status = await self.get_appliance(said)
            raw = self._attr_from_appliance_payload(status, f"{timer_attr}_SetTimeSet")
            seconds = int(raw or 0)
        except (WhirlpoolApiError, TypeError, ValueError):
            seconds = 0

        return await self.send_attributes(
            said,
            {
                f"{timer_attr}_SetTimeSet": str(seconds),
                f"{timer_attr}_SetOperations": "1",
            },
        )

    async def check_firmware_update(self, said: str) -> Any:
        """Check whether Whirlpool reports a firmware update.

        Captured Android app 6.8.3:
          GET /api/v2/checkForFirmwareUpdate/{SAID}
          -> {"FirmwareUpdateAvailable": false}
        """
        return await self.request("GET", f"/api/v2/checkForFirmwareUpdate/{said}")

    async def firmware_update_available(self, said: str) -> bool | None:
        """Return firmware-update availability as a boolean."""
        payload = await self.check_firmware_update(said)
        if isinstance(payload, Mapping):
            value = payload.get("FirmwareUpdateAvailable")
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "available"}
            if value is not None:
                return bool(value)
        return None

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

    @staticmethod
    def _extract_datetime_string(payload: Any, said: str | None = None) -> str | None:
        """Extract a Whirlpool date/time string from a localized-time payload."""
        datetime_re = re.compile(
            r"\d{4}-\s*\d{1,2}-\d{1,2}T\s*\d{1,2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?"
        )

        if isinstance(payload, str):
            match = datetime_re.search(payload)
            return match.group(0) if match else None

        if isinstance(payload, list):
            for item in payload:
                found = WhirlpoolCloudClient._extract_datetime_string(item, said)
                if found:
                    return found
            return None

        if not isinstance(payload, Mapping):
            return None

        # Prefer a dict associated with this appliance if the endpoint returns
        # more than one SAID.
        if said:
            for key, value in payload.items():
                if str(key).upper() == str(said).upper():
                    found = WhirlpoolCloudClient._extract_datetime_string(value, said)
                    if found:
                        return found

        preferred_keys = (
            "dateTime",
            "datetime",
            "dateTimeSet",
            "localizedDateTime",
            "localizedDateAndTime",
            "localDateTime",
            "time",
            "currentTime",
            "XCat_DateTimeSetDateTimeSet",
        )
        for key in preferred_keys:
            if key in payload:
                found = WhirlpoolCloudClient._extract_datetime_string(payload[key], said)
                if found:
                    return found

        for value in payload.values():
            found = WhirlpoolCloudClient._extract_datetime_string(value, said)
            if found:
                return found

        return None

    async def _apk_localized_datetime_for_said(self, said: str) -> str | None:
        """Best-effort call to APK-observed localized date/time endpoints.

        The APK string table contains both ``getLocalizedDateAndTimeForSaids``
        and ``localizedDateAndTimeForSaids``. Builds vary on method/body shape,
        so try the harmless variants and use the first recognizable datetime
        response.
        """
        attempts: tuple[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None], ...] = (
            ("GET", "/api/v1/getLocalizedDateAndTimeForSaids", None, {"saids": said}),
            ("GET", "/api/v1/getLocalizedDateAndTimeForSaids", None, {"said": said}),
            ("POST", "/api/v1/getLocalizedDateAndTimeForSaids", {"saids": [said]}, None),
            ("POST", "/api/v1/getLocalizedDateAndTimeForSaids", {"said": said}, None),
            ("GET", "/api/v1/localizedDateAndTimeForSaids", None, {"saids": said}),
            ("POST", "/api/v1/localizedDateAndTimeForSaids", {"saids": [said]}, None),
            ("POST", "/api/v1/localizedDateAndTimeForSaids", {"said": said}, None),
        )
        for method, path, body, params in attempts:
            try:
                payload = await self.request(method, path, json=body, params=params)
            except WhirlpoolApiError as err:
                _LOGGER.debug("Localized date/time endpoint %s %s failed for %s: %s", method, path, said, err)
                continue

            found = self._extract_datetime_string(payload, said)
            if found:
                _LOGGER.debug("Using Whirlpool localized appliance time for %s from %s %s: %s", said, method, path, found)
                return found

            _LOGGER.debug("Localized date/time endpoint %s %s returned no parseable datetime for %s: %r", method, path, said, payload)
        return None

    @staticmethod
    def _payload_looks_like_minerva_cooking(payload: Any) -> bool:
        """Return true for Minerva cooking/oven payloads that reject time writes."""
        text = str(payload).lower()
        return "cooking" in text or "minerva" in text or "ovenuppercavity" in text or "mwo_" in text

    async def _set_appliance_time_mode(self, said: str, mode: str, timezone_name: str | None = None) -> Any:
        """Set appliance time auto-update mode using the Whirlpool Android app flow.

        Captured Android app 6.8.3 behavior:
          * Auto update ON sends dateTimeMode "2"
          * Auto update OFF sends dateTimeMode "4"

        Both use:
          POST /api/v1/localizedDateAndTimeForSaids
          {"attributes": [{"said": SAID,
                           "dateTimeSetDateTimeSet": "YYYY-MM-DDTHH:MM:SS+00:00",
                           "dateTimeMode": MODE}]}
        """
        status = await self.get_appliance(said)

        detected_tz = timezone_name or self._attr_from_appliance_payload(status, "TimeZoneId")
        detected_tz = detected_tz or self._attr_from_appliance_payload(status, "TimezoneId")

        try:
            ldt_payload = await self.request(
                "POST",
                "/api/v1/getLocalizedDateAndTimeForSaids",
                json={"saidList": [said]},
            )
            if isinstance(ldt_payload, Mapping):
                ldt_info = ldt_payload.get("ldtInfo")
                if isinstance(ldt_info, list):
                    for item in ldt_info:
                        if not isinstance(item, Mapping):
                            continue
                        if str(item.get("SAID") or item.get("said") or "").upper() == str(said).upper():
                            detected_tz = item.get("TIME_ZONE") or item.get("timeZone") or detected_tz
                            break
        except WhirlpoolApiError as err:
            _LOGGER.debug("Unable to fetch localized date/time context for %s: %s", said, err)

        detected_tz = detected_tz or "UTC"
        try:
            tzinfo = ZoneInfo(str(detected_tz))
        except ZoneInfoNotFoundError:
            _LOGGER.warning("Unknown appliance timezone %r for %s; falling back to UTC", detected_tz, said)
            tzinfo = ZoneInfo("UTC")

        now_utc = datetime.now(tzinfo).replace(microsecond=0).astimezone(timezone.utc)
        payload = {
            "attributes": [
                {
                    "said": said,
                    "dateTimeSetDateTimeSet": now_utc.isoformat(),
                    "dateTimeMode": str(mode),
                }
            ]
        }
        return await self.request("POST", "/api/v1/localizedDateAndTimeForSaids", json=payload)

    async def sync_appliance_time(self, said: str, timezone_name: str | None = None) -> Any:
        """Enable Whirlpool app-style automatic time update.

        The Whirlpool app's Auto Update Time ON path sends dateTimeMode "2".
        """
        return await self._set_appliance_time_mode(said, "2", timezone_name)

    async def set_time_auto_update(self, said: str, enabled: bool, timezone_name: str | None = None) -> Any:
        """Turn Whirlpool app-style automatic time update on or off."""
        return await self._set_appliance_time_mode(said, "2" if enabled else "4", timezone_name)

    async def set_microwave_turntable(self, said: str, enabled: bool) -> Any:
        return await self.send_attributes(said, {"Mwo_CycleSetTurntable": "1" if enabled else "0"})

    async def set_oven_control_lock(self, said: str, locked: bool) -> Any:
        return await self.send_attributes(said, {"Sys_OperationSetControlLock": "1" if locked else "0"})

    async def set_remote_enable(self, said: str, enabled: bool) -> Any:
        """Attempt to toggle remote enable using the Whirlpool remote-enable attribute.

        Some Whirlpool/Minerva data models expose XCat_RemoteSetRemoteControlEnable
        as read-only to the cloud, but add this for appliances/app builds that
        accept it and for troubleshooting stale remote-enable cloud state.
        """
        return await self.send_attributes(said, {"XCat_RemoteSetRemoteControlEnable": "1" if enabled else "0"})

    async def set_oven_sabbath_mode(self, said: str, enabled: bool) -> Any:
        return await self.send_attributes(said, {"Sys_OperationSetSabbathModeEnabled": "1" if enabled else "0"})

    async def set_quiet_mode(self, said: str, enabled: bool) -> Any:
        """Turn appliance quiet mode on/off using app-observed attribute."""
        return await self.send_attributes(said, {"Sys_OperationSetQuietModeEnabled": "1" if enabled else "0"})


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


def appliance_ddm_key(appliance: Mapping[str, Any]) -> str | None:
    """Return the DDM/data-model key used by Whirlpool's capability endpoint."""
    value = _first_value(appliance, DDM_KEYS)
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
