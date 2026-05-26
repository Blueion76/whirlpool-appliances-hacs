"""DDM/capability parsing for Whirlpool Appliances."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

COMMON_MODE_ENUM_TO_PRESET = {
    "CommonModeIdle": "Standby",
    "CommonModeBake": "Bake",
    "CommonModeConvectBake": "Convection Bake",
    "CommonModeBroil": "Broil",
    "CommonModeConvectBroil": "Convection Broil",
    "CommonModeConvectRoast": "Convection Roast",
    "CommonModeKeepWarm": "Keep Warm",
    "CommonModeAirFry": "Air Fry",
    "CommonModeSabbathBake": "Sabbath Bake",
}
PRESET_TO_SERVICE_MODE = {
    "Standby": "standby",
    "Bake": "bake",
    "Convection Bake": "convect_bake",
    "Broil": "broil",
    "Convection Broil": "convect_broil",
    "Convection Roast": "convect_roast",
    "Keep Warm": "keep_warm",
    "Air Fry": "air_fry",
    "Sabbath Bake": "sabbath_bake",
}
FROZEN_BAKE_ENUM_TO_FOOD = {
    "FrozenBakeFoodMeals": "meals",
    "FrozenBakeFoodNuggets": "nuggets",
    "FrozenBakeFoodLasagna": "lasagna",
    "FrozenBakeFoodPizza": "pizza",
    "FrozenBakeFoodPie": "pie",
    "FrozenBakeFoodFries": "fries",
}


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


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_ddm_documents(payload: Any) -> Iterable[Mapping[str, Any]]:
    """Yield actual per-appliance DDM documents from several Whirlpool response shapes."""
    if not isinstance(payload, Mapping):
        return

    # /api/v2/DeviceDataModel returns {"SAID": {"dataModel": ..., "personality": ...}}
    for value in payload.values():
        if isinstance(value, Mapping) and "dataModel" in value:
            yield value

    # Future/alternate shapes.
    if "dataModel" in payload:
        yield payload
    for key in ("data", "items", "results", "dataModels"):
        value = payload.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping) and "dataModel" in item:
                    yield item
        elif isinstance(value, Mapping) and "dataModel" in value:
            yield value


def _attribute_summary(attr: Mapping[str, Any]) -> dict[str, Any]:
    enum_values = attr.get("EnumValues") if isinstance(attr.get("EnumValues"), Mapping) else {}
    range_values = attr.get("RangeValues") if isinstance(attr.get("RangeValues"), Mapping) else {}
    return {
        "mapped_name": attr.get("MappedAttributeName") or attr.get("M2MAttributeName") or attr.get("AttributeName"),
        "m2m_name": attr.get("M2MAttributeName"),
        "attribute_name": attr.get("AttributeName"),
        "instance": attr.get("Instance"),
        "device_io": attr.get("DeviceIO"),
        "data_type": attr.get("DataType"),
        "default": attr.get("Default"),
        "key": attr.get("Key"),
        "range": {
            "min": range_values.get("Min"),
            "max": range_values.get("Max"),
            "step": range_values.get("StepSize") or range_values.get("Step"),
        } if range_values else None,
        "enum_values": {str(k): v for k, v in enum_values.items()} if enum_values else None,
    }


def _attribute_map(documents: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for document in documents:
        attrs = ((document.get("dataModel") or {}).get("attributes") or [])
        if not isinstance(attrs, list):
            continue
        for attr in attrs:
            if not isinstance(attr, Mapping):
                continue
            summary = _attribute_summary(attr)
            name = summary.get("mapped_name")
            if name:
                out[str(name)] = summary
    return out


def _enum_code_for_value(attr_map: Mapping[str, Mapping[str, Any]], attr_name: str, enum_value: str) -> str | None:
    enum_values = (attr_map.get(attr_name) or {}).get("enum_values") or {}
    if not isinstance(enum_values, Mapping):
        return None
    for code, value in enum_values.items():
        if value == enum_value:
            return str(code)
    return None


def _temp_range_summary(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    min_tenths = _as_int(raw.get("Min"))
    max_tenths = _as_int(raw.get("Max"))
    default_tenths = _as_int(raw.get("Default"))
    return {
        "min_tenths_c": min_tenths,
        "max_tenths_c": max_tenths,
        "default_tenths_c": default_tenths,
        "min_c": (min_tenths / 10) if min_tenths is not None else None,
        "max_c": (max_tenths / 10) if max_tenths is not None else None,
        "default_c": (default_tenths / 10) if default_tenths is not None else None,
        "step_c": _as_float(raw.get("StepC") or raw.get("Step")),
        "step_f": _as_float(raw.get("StepF")),
    }


def _range_summary(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    return {
        "min": _as_int(raw.get("Min")),
        "max": _as_int(raw.get("Max")),
        "step": _as_int(raw.get("Step") or raw.get("StepSize")),
        "default": _as_int(raw.get("Default")),
    }


def _extract_frozen_bake(
    set_cycle: Mapping[str, Any],
    capability_data: Mapping[str, Any],
    attr_map: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Extract Frozen Bake food modes and ranges from DDM personality."""
    automatic = set_cycle.get("homeAutomaticOven")
    if not isinstance(automatic, Mapping):
        return {"foods": [], "food_by_name": {}}

    foods: list[dict[str, Any]] = []
    for food_key, food_payload in automatic.items():
        if not isinstance(food_payload, Mapping):
            continue
        attrs = food_payload.get("Attributes")
        if not isinstance(attrs, Mapping):
            continue

        food_attr = None
        enum_value = None
        for attr_name, values in attrs.items():
            if "FrozenBakeFood" not in str(attr_name):
                continue
            food_attr = str(attr_name)
            if isinstance(values, list) and values:
                enum_value = str(values[0])
            elif isinstance(values, str):
                enum_value = values
            break
        if not food_attr or not enum_value:
            continue

        food = FROZEN_BAKE_ENUM_TO_FOOD.get(enum_value)
        if not food:
            continue

        details = capability_data.get(food_key) if isinstance(capability_data.get(food_key), Mapping) else {}
        required = details.get("Required") if isinstance(details, Mapping) else {}

        temp_range = None
        cook_time_range = None
        complete_action = None
        if isinstance(required, Mapping):
            for req_attr, req_details in required.items():
                if not isinstance(req_details, Mapping):
                    continue
                if "TargetTemp" in str(req_attr):
                    temp_range = _temp_range_summary(req_details.get("TempRange"))
                elif "CookTime" in str(req_attr):
                    cook_time_range = _range_summary(req_details.get("Range"))
                elif "CookTimeCompleteAction" in str(req_attr):
                    complete_action = {
                        "options": req_details.get("Enumeration"),
                        "default": req_details.get("Default"),
                    }

        foods.append(
            {
                "food": food,
                "food_key": food_key,
                "food_attribute": food_attr,
                "enum_value": enum_value,
                "code": _enum_code_for_value(attr_map, food_attr, enum_value),
                "name": food_payload.get("Name"),
                "target_temperature": temp_range,
                "cook_time": cook_time_range,
                "cook_time_complete_action": complete_action,
            }
        )

    order = {"pizza": 0, "pie": 1, "meals": 2, "fries": 3, "nuggets": 4, "lasagna": 5}
    foods.sort(key=lambda item: order.get(str(item.get("food")), 50))
    return {
        "foods": foods,
        "food_by_name": {str(item["food"]): item for item in foods if item.get("food")},
    }


def _extract_cooking_capabilities(documents: list[Mapping[str, Any]], attr_map: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Extract app-visible oven modes and per-mode ranges from DDM personality."""
    cooking: dict[str, Any] = {"cavities": {}}

    for document in documents:
        personality = document.get("personality")
        if not isinstance(personality, Mapping):
            continue
        capability_entries = personality.get("capability")
        if not isinstance(capability_entries, list):
            continue

        for entry in capability_entries:
            root = entry.get("Capability") if isinstance(entry, Mapping) else None
            if not isinstance(root, Mapping):
                continue

            for cavity_key, cavity_payload in root.items():
                if not isinstance(cavity_payload, Mapping) or "Cavity" not in str(cavity_key):
                    continue

                set_cycle = cavity_payload.get("SetCycle")
                capability_data = cavity_payload.get("CapabilityData")
                if not isinstance(set_cycle, Mapping) or not isinstance(capability_data, Mapping):
                    continue

                manual_cycles = set_cycle.get("homeManualOven")
                if not isinstance(manual_cycles, Mapping):
                    continue

                modes: list[dict[str, Any]] = []
                for mode_key, mode_payload in manual_cycles.items():
                    if mode_key == "Default" or not isinstance(mode_payload, Mapping):
                        continue

                    attrs = mode_payload.get("Attributes")
                    if not isinstance(attrs, Mapping):
                        continue

                    mode_attr = None
                    enum_value = None
                    for attr_name, values in attrs.items():
                        if "CommonMode" not in str(attr_name):
                            continue
                        mode_attr = str(attr_name)
                        if isinstance(values, list) and values:
                            enum_value = str(values[0])
                        elif isinstance(values, str):
                            enum_value = values
                        break
                    if not mode_attr or not enum_value:
                        continue

                    preset = COMMON_MODE_ENUM_TO_PRESET.get(enum_value)
                    if not preset or preset in {"Standby", "Sabbath Bake"}:
                        continue

                    details = capability_data.get(mode_key) if isinstance(capability_data.get(mode_key), Mapping) else {}
                    required = details.get("Required") if isinstance(details, Mapping) else {}
                    optional = details.get("Optional") if isinstance(details, Mapping) else {}

                    temp_range = None
                    cook_time_range = None
                    delay_time_range = None
                    complete_action = None

                    if isinstance(required, Mapping):
                        for req_attr, req_details in required.items():
                            if not isinstance(req_details, Mapping):
                                continue
                            if "TargetTemp" in str(req_attr):
                                temp_range = _temp_range_summary(req_details.get("TempRange"))
                            elif "CookTime" in str(req_attr):
                                cook_time_range = _range_summary(req_details.get("Range"))
                            elif "CookTimeCompleteAction" in str(req_attr):
                                complete_action = {
                                    "options": req_details.get("Enumeration"),
                                    "default": req_details.get("Default"),
                                }
                    if isinstance(optional, Mapping):
                        for opt_attr, opt_details in optional.items():
                            if not isinstance(opt_details, Mapping):
                                continue
                            if "CookTime" in str(opt_attr):
                                cook_time_range = cook_time_range or _range_summary(opt_details.get("Range"))
                            elif "DelayTime" in str(opt_attr):
                                delay_time_range = _range_summary(opt_details.get("Range"))

                    modes.append(
                        {
                            "preset": preset,
                            "service_mode": PRESET_TO_SERVICE_MODE.get(preset),
                            "mode_key": mode_key,
                            "mode_attribute": mode_attr,
                            "enum_value": enum_value,
                            "code": _enum_code_for_value(attr_map, mode_attr, enum_value),
                            "name": mode_payload.get("Name"),
                            "target_temperature": temp_range,
                            "cook_time": cook_time_range,
                            "delay_time": delay_time_range,
                            "cook_time_complete_action": complete_action,
                        }
                    )

                if not modes:
                    continue

                # Keep DDM/app ordering when possible, but make common app order stable.
                order = {"Bake": 0, "Broil": 1, "Keep Warm": 2}
                modes.sort(key=lambda item: order.get(str(item.get("preset")), 50))

                default_key = set_cycle.get("Default")
                default_preset = None
                if isinstance(default_key, str):
                    for mode in modes:
                        if mode.get("mode_key") == default_key:
                            default_preset = mode.get("preset")
                            break

                frozen_bake = _extract_frozen_bake(set_cycle, capability_data, attr_map)

                cooking["cavities"][str(cavity_key)] = {
                    "supported_presets": [str(mode["preset"]) for mode in modes],
                    "supported_service_modes": [str(mode["service_mode"]) for mode in modes if mode.get("service_mode")],
                    "code_by_preset": {
                        str(mode["preset"]): str(mode["code"])
                        for mode in modes
                        if mode.get("code") is not None
                    },
                    "mode_by_preset": {str(mode["preset"]): mode for mode in modes},
                    "default_preset": default_preset,
                    "modes": modes,
                    "frozen_bake": frozen_bake,
                }

    return cooking


def _feature_support_from_attrs(attr_names: set[str]) -> dict[str, bool]:
    lowered_blob = "\n".join(sorted(attr_names)).lower()
    return {
        feature: any(keyword in lowered_blob for keyword in keywords)
        for feature, keywords in FEATURE_KEYWORDS.items()
    }


def parse_ddm_capabilities(payload: Any) -> dict[str, Any]:
    """Return an integration-oriented summary of a Whirlpool DDM payload.

    This parser understands the live /api/v2/DeviceDataModel response shape and
    preserves a safe fallback string scan for unknown schemas.
    """
    documents = list(_iter_ddm_documents(payload))
    attr_map = _attribute_map(documents)

    flattened = _walk(payload)
    attr_names: set[str] = set(attr_map)
    numeric_hints: dict[str, Any] = {}

    for path, value in flattened:
        attr = _clean_attr(value)
        if attr:
            attr_names.add(attr)

        leaf = path.rsplit(".", 1)[-1].lower()
        if leaf in {"min", "minimum", "max", "maximum", "default", "step", "stepsize", "stepc", "stepf", "increment"}:
            if isinstance(value, (int, float, str)) and str(value).strip() != "":
                numeric_hints[path] = value

    readable_attrs = sorted(
        name for name, attr in attr_map.items()
        if str(attr.get("device_io") or "").upper() in {"RO", "RW"}
    )
    writable_attrs = sorted(
        name for name, attr in attr_map.items()
        if str(attr.get("device_io") or "").upper() in {"WO", "RW"} or str(attr.get("device_io") or "") == ""
    )
    enum_attrs = {
        name: attr["enum_values"]
        for name, attr in attr_map.items()
        if attr.get("enum_values")
    }
    range_attrs = {
        name: attr["range"]
        for name, attr in attr_map.items()
        if attr.get("range")
    }

    cooking = _extract_cooking_capabilities(documents, attr_map)

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
        "schema": "device_data_model_v2" if documents else "unknown",
        "document_count": len(documents),
        "attribute_count": len(attr_names),
        "attributes": sorted(attr_names)[:500],
        "attributes_truncated": max(len(attr_names) - 500, 0),
        "attribute_map": attr_map,
        "readable_attributes": readable_attrs,
        "writable_attributes": writable_attrs,
        "enum_attributes": enum_attrs,
        "range_attributes": range_attrs,
        "supported_features": _feature_support_from_attrs(attr_names),
        "cooking": cooking,
        "likely_modes_or_cycles": likely_modes[:200],
        "likely_temperature_attributes": likely_temperature_attrs[:100],
        "numeric_hints": dict(list(numeric_hints.items())[:300]),
        "numeric_hints_truncated": max(len(numeric_hints) - 300, 0),
    }


def cooking_cavity_capability(parsed: Mapping[str, Any] | None, cavity: str | None = "upper") -> Mapping[str, Any] | None:
    """Return parsed cooking capability for a cavity."""
    if not isinstance(parsed, Mapping):
        return None
    cooking = parsed.get("cooking")
    if not isinstance(cooking, Mapping):
        return None
    cavities = cooking.get("cavities")
    if not isinstance(cavities, Mapping):
        return None

    prefix = "OvenLowerCavity" if str(cavity).lower().startswith("lower") else "OvenUpperCavity"
    for key, value in cavities.items():
        if str(key).startswith(prefix) and isinstance(value, Mapping):
            return value
    return None
