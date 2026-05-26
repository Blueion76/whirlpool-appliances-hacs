"""Best-effort DDM/capability parsing for Whirlpool Appliances."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _walk(value: Any, *, path: str = "") -> list[tuple[str, Any]]:
    """Flatten a nested DDM payload into (path, value) pairs."""
    items: list[tuple[str, Any]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            next_path = f"{path}.{key}" if path else str(key)
            items.extend(_walk(item, path=next_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            items.extend(_walk(item, path=f"{path}.{idx}"))
    else:
        items.append((path, value))
    return items


def _clean_attr(value: Any) -> str | None:
    """Return strings that look like DDM/Whirlpool attribute identifiers."""
    if not isinstance(value, str):
        return None
    if len(value) < 3 or len(value) > 120:
        return None
    lowered = value.lower()
    if any(token in lowered for token in ("http://", "https://", "jpg", "png", "svg")):
        return None
    if any(token in value for token in ("_", "Cavity", "Cycle", "Mode", "Status", "Set", "Mwo", "Sys", "XCat")):
        return value
    if lowered in {
        "remoteenable",
        "remoteenabled",
        "controllock",
        "hmicontrollockout",
        "sabbathmode",
        "cavitylight",
        "machinestate",
        "doorstatus",
        "doorlockstatus",
        "activefault",
        "cycle",
        "phasename",
    }:
        return value
    return None


FEATURE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "remote_enable": ("remoteenable", "remoteenabled", "remotecontrol"),
    "control_lock": ("controllock", "hmicontrollockout", "operationsetcontrollock"),
    "sabbath_mode": ("sabbathmode", "sabbath"),
    "cavity_light": ("cavitylight", "displaysetlighton", "lighton"),
    "quiet_mode": ("quietmode", "operationsetquietmodeenabled"),
    "kitchen_timer": ("kitchentimer", "timer"),
    "oven": ("ovencavity", "cavitytargettemp", "cavityactualtemp", "commonmode"),
    "microwave": ("mwo_", "microwave", "turntable", "cookpower"),
    "laundry": ("washer", "dryer", "machinestate", "cyclephase", "soil", "spin"),
    "dishwasher": ("dish", "dishwasher"),
    "refrigeration": ("refrigerator", "freezer", "icemaker", "waterfilter", "vacation"),
    "air_conditioner": ("airconditioner", "acfilter", "fanspeed", "louver", "swing"),
    "firmware": ("firmware", "ota"),
}


def parse_ddm_capabilities(payload: Any) -> dict[str, Any]:
    """Return a compact, integration-oriented summary of a DDM payload.

    The endpoint is model/DDM-specific and Whirlpool has multiple schemas. This
    parser is intentionally conservative: it does not attempt to turn unknown
    DDM structures into writable controls. It only summarizes discovered strings,
    likely feature support, and obvious min/max/default/step metadata for
    diagnostics and future appliance support.
    """
    flattened = _walk(payload)
    attr_names: set[str] = set()
    numeric_hints: dict[str, Any] = {}

    for path, value in flattened:
        attr = _clean_attr(value)
        if attr:
            attr_names.add(attr)

        leaf = path.rsplit(".", 1)[-1].lower()
        if leaf in {"min", "minimum", "max", "maximum", "default", "step", "increment"}:
            if isinstance(value, (int, float, str)) and str(value).strip() != "":
                numeric_hints[path] = value

    lowered_blob = "\n".join(sorted(attr_names)).lower()
    supported_features = {
        feature: any(keyword in lowered_blob for keyword in keywords)
        for feature, keywords in FEATURE_KEYWORDS.items()
    }

    likely_modes = sorted(
        value
        for value in attr_names
        if any(token in value.lower() for token in ("mode", "cycle", "operation"))
    )

    likely_temperature_attrs = sorted(
        value
        for value in attr_names
        if any(token in value.lower() for token in ("temp", "temperature", "targettemp", "actualtemp"))
    )

    return {
        "attribute_count": len(attr_names),
        "attributes": sorted(attr_names)[:500],
        "attributes_truncated": max(len(attr_names) - 500, 0),
        "supported_features": supported_features,
        "likely_modes_or_cycles": likely_modes[:200],
        "likely_temperature_attributes": likely_temperature_attrs[:100],
        "numeric_hints": dict(list(numeric_hints.items())[:200]),
        "numeric_hints_truncated": max(len(numeric_hints) - 200, 0),
    }
