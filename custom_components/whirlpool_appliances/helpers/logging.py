"""Logging helpers for Whirlpool Appliances integration."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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


def redact(value: Any, *, parent_key: str = "") -> Any:
    """Recursively redact secrets and identifiers before logging."""
    key_lower = parent_key.lower()
    if any(part in key_lower for part in SENSITIVE_KEY_PARTS):
        if value in (None, "", [], {}):
            return value
        if isinstance(value, Mapping):
            return {str(key): REDACTED for key in value}
        if isinstance(value, list):
            return [REDACTED for _ in value]
        if isinstance(value, tuple):
            return tuple(REDACTED for _ in value)
        return REDACTED

    if isinstance(value, Mapping):
        return {str(key): redact(item, parent_key=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, parent_key=parent_key) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, parent_key=parent_key) for item in value)
    return value


def summarize(value: Any, *, max_items: int = 30, max_string: int = 300) -> Any:
    """Return a small, log-safe preview of a payload."""
    value = redact(value)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= max_items:
                out["__truncated__"] = f"{len(value) - max_items} more keys"
                break
            out[str(key)] = summarize(item, max_items=max_items, max_string=max_string)
        return out
    if isinstance(value, list):
        out = [summarize(item, max_items=max_items, max_string=max_string) for item in value[:max_items]]
        if len(value) > max_items:
            out.append({"__truncated__": f"{len(value) - max_items} more items"})
        return out
    if isinstance(value, tuple):
        return tuple(summarize(item, max_items=max_items, max_string=max_string) for item in value[:max_items])
    if isinstance(value, str) and len(value) > max_string:
        return f"{value[:max_string]}… <truncated {len(value) - max_string} chars>"
    return value


def summarize_keys(value: Any) -> str:
    """Summarize the top-level shape of a value for normal debug logs."""
    value = redact(value)
    if isinstance(value, Mapping):
        keys = list(value.keys())
        preview = ", ".join(str(key) for key in keys[:20])
        suffix = f", +{len(keys) - 20} more" if len(keys) > 20 else ""
        return f"dict[{len(keys)}]({preview}{suffix})"
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return f"{type(value).__name__}[{len(value)}]"
    return type(value).__name__
