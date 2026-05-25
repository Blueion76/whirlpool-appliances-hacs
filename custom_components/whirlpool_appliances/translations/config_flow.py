"""Config flow for Whirlpool Appliances integration."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from aiohttp import ClientError

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_USERNAME
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import WhirlpoolAccountLockedError, WhirlpoolApiError, WhirlpoolAuthError, WhirlpoolCloudClient
from .const import (
    CONF_BRAND,
    CONF_ENABLE_CONTROL_ENTITIES,
    CONF_EXPOSE_RAW_SENSORS,
    CONF_SCAN_INTERVAL,
    DEFAULT_BRAND,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SUPPORTED_BRANDS,
    SUPPORTED_REGIONS,
)


async def _validate_login(hass, data: dict[str, Any], *, check_appliances_exist: bool) -> str | None:
    """Validate credentials using the same high-level behavior as official HA Whirlpool.

    The official integration authenticates first, then only treats appliance discovery
    as a setup failure when no supported appliances are returned.
    """
    session = async_get_clientsession(hass)
    client = WhirlpoolCloudClient(
        session,
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        region=data.get(CONF_REGION, DEFAULT_REGION),
        brand=data.get(CONF_BRAND, DEFAULT_BRAND),
    )
    try:
        await client.login()
        if check_appliances_exist and not await client.list_appliances():
            return "no_appliances"
    except WhirlpoolAccountLockedError:
        return "account_locked"
    except WhirlpoolAuthError:
        return "invalid_auth"
    except (WhirlpoolApiError, ClientError, TimeoutError):
        return "cannot_connect"
    except Exception:  # noqa: BLE001
        return "unknown"
    return None


class WhirlpoolApkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Whirlpool Appliances config flow."""

    VERSION = 1

    async def async_step_reauth(self, entry_data: Mapping[str, Any]):
        """Handle re-authentication."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None):
        """Confirm re-authentication with Whirlpool."""
        errors: dict[str, str] = {}
        reauth_entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**reauth_entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD], CONF_BRAND: user_input[CONF_BRAND]}
            error = await _validate_login(self.hass, data, check_appliances_exist=False)
            if error is None:
                return self.async_update_reload_and_abort(reauth_entry, data=data)
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str, vol.Required(CONF_BRAND, default=reauth_entry.data.get(CONF_BRAND, DEFAULT_BRAND)): vol.In(SUPPORTED_BRANDS)}),
            errors=errors,
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await _validate_login(self.hass, user_input, check_appliances_exist=True)
            if error is None:
                await self.async_set_unique_id(user_input[CONF_USERNAME].lower(), raise_on_progress=False)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=user_input[CONF_USERNAME], data=user_input)
            errors["base"] = error

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required(CONF_REGION, default=DEFAULT_REGION): vol.In(SUPPORTED_REGIONS),
                vol.Required(CONF_BRAND, default=DEFAULT_BRAND): vol.In(SUPPORTED_BRANDS),
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
                vol.Optional(CONF_EXPOSE_RAW_SENSORS, default=True): bool,
                vol.Optional(CONF_ENABLE_CONTROL_ENTITIES, default=False): bool,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
