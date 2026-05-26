"""Diagnostics support for Whirlpool Appliances integration."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import appliance_ddm_key, appliance_name, appliance_said
from .const import DATA_COORDINATOR, DOMAIN

REDACTED = "**REDACTED**"

SENSITIVE_KEY_PARTS = (
    "token",
    "authorization",
    "password",
    "secret",
    "session",
    "access_key",
    "refresh",
    "email",
    "username",
    "user_name",
    "userid",
    "user_id",
    "accountid",
    "account_id",
    "identityid",
    "identity_id",
    "cognito",
    "mac",
    "macaddress",
    "mac_address",
    "serial",
    "said",
    "applianceid",
    "appliance_id",
    "deviceid",
    "device_id",
    "thingid",
    "thing_id",
    "locationid",
    "location_id",
)


def _redact(value: Any, *, parent_key: str = "") -> Any:
    """Recursively redact user/device-identifying data from diagnostics."""
    key_lower = parent_key.lower()
    if any(part in key_lower for part in SENSITIVE_KEY_PARTS):
        if value in (None, "", [], {}):
            return value
        if isinstance(value, list):
            return [REDACTED for _ in value]
        if isinstance(value, tuple):
            return tuple(REDACTED for _ in value)
        if isinstance(value, Mapping):
            return {str(key): REDACTED for key in value}
        return REDACTED

    if isinstance(value, Mapping):
        return {str(key): _redact(item, parent_key=str(key)) for key, item in value.items()}

    if isinstance(value, list):
        return [_redact(item, parent_key=parent_key) for item in value]

    if isinstance(value, tuple):
        return tuple(_redact(item, parent_key=parent_key) for item in value)

    return value


def _safe_preview(value: Any, *, max_items: int = 50) -> Any:
    """Keep diagnostics useful while avoiding very large DDM/status dumps in summaries."""
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= max_items:
                out["__truncated__"] = f"{len(value) - max_items} more keys"
                break
            out[str(key)] = _safe_preview(item, max_items=max_items)
        return out
    if isinstance(value, list):
        return [_safe_preview(item, max_items=max_items) for item in value[:max_items]]
    return value


def _metadata_for_appliance(appliance: Mapping[str, Any], status: Any | None) -> dict[str, Any]:
    """Return non-secret metadata that helps add support for more appliances."""
    return {
        "name": appliance_name(appliance),
        "said_present": bool(appliance_said(appliance)),
        "ddm_key": appliance_ddm_key(appliance),
        "category": (
            appliance.get("CATEGORY_NAME")
            or appliance.get("categoryName")
            or appliance.get("category")
            or appliance.get("applianceCategory")
            or appliance.get("applianceType")
        ),
        "model": appliance.get("MODEL_NO") or appliance.get("modelNumber") or appliance.get("model"),
        "ccuri": appliance.get("ccuri") or appliance.get("CC_URI"),
        "data_model_key": appliance.get("DATA_MODEL_KEY") or appliance.get("dataModelKey"),
        "thing_shield": bool(appliance.get("thingShield")),
        "source": appliance.get("source"),
        "status_type": type(status).__name__ if status is not None else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return sanitized diagnostics for unsupported appliance development."""
    coordinator = entry.runtime_data or hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get(DATA_COORDINATOR)
    data = coordinator.data or {}

    appliances: Sequence[Mapping[str, Any]] = data.get("appliances") or []
    statuses: Mapping[str, Any] = data.get("statuses") or {}
    ddm_capabilities = data.get("ddm_capabilities") or getattr(coordinator, "_ddm_capabilities", {})
    ddm_errors = data.get("ddm_errors") or getattr(coordinator, "_ddm_errors", {})
    appliance_metadata = data.get("appliance_metadata") or getattr(coordinator, "_appliance_metadata", {})

    appliance_reports: list[dict[str, Any]] = []
    for appliance in appliances:
        said = appliance_said(appliance)
        status = statuses.get(said) if said else None
        appliance_reports.append(
            {
                "metadata": _redact(_metadata_for_appliance(appliance, status)),
                "appliance": _redact(appliance),
                "raw_status": _redact(status),
            }
        )

    return {
        "integration": {
            "domain": DOMAIN,
            "version": entry.version,
            "title": entry.title,
        },
        "appliance_count": len(appliances),
        "appliance_metadata": _redact(appliance_metadata),
        "appliances": appliance_reports,
        "ddm_capabilities": _redact(ddm_capabilities),
        "ddm_errors": _redact(ddm_errors),
        "notes": [
            "SAIDs, serials, account IDs, tokens, MAC addresses, and location IDs are redacted.",
            "DDM capability payloads come from /api/v1/contents/all/{ddmKey} when the appliance exposes a DDM key.",
            "Upload this diagnostics file with unsupported-appliance issues so support can be added safely.",
        ],
    }
