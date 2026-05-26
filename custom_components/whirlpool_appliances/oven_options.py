"""Shared local oven command option helpers."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .entity import attr_value

OVEN_MODE_CODE_TO_SERVICE = {
    "2": "bake",
    "6": "convect_bake",
    "8": "broil",
    "9": "convect_broil",
    "16": "convect_roast",
    "24": "keep_warm",
    "41": "air_fry",
}
OVEN_SERVICE_TO_NAME = {
    "bake": "Bake",
    "convect_bake": "Convection Bake",
    "broil": "Broil",
    "convect_broil": "Convection Broil",
    "convect_roast": "Convection Roast",
    "keep_warm": "Keep Warm",
    "air_fry": "Air Fry",
}
OVEN_NAME_TO_SERVICE = {name: service for service, name in OVEN_SERVICE_TO_NAME.items()}

OVEN_COMPLETE_ACTION_CODE_TO_SERVICE = {
    "1": "stay_on",
    "2": "keep_warm",
    "3": "turn_off",
}
OVEN_COMPLETE_ACTION_SERVICE_TO_NAME = {
    "stay_on": "Stay On",
    "keep_warm": "Keep Warm",
    "turn_off": "Turn Off",
}
OVEN_COMPLETE_ACTION_NAME_TO_SERVICE = {
    name: service for service, name in OVEN_COMPLETE_ACTION_SERVICE_TO_NAME.items()
}

FROZEN_BAKE_FOOD_OPTIONS = ["None", "pizza", "pie", "meals", "fries", "nuggets", "lasagna"]


def cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def oven_is_active(flat: Mapping[str, Any], cavity: str | None) -> bool:
    return str(attr_value(flat, f"{cavity_prefix(cavity)}_OpStatusState") or "") in {"1", "2"}


def temp_from_tenths(value: Any) -> float | None:
    if value in (None, "", "0", 0):
        return None
    try:
        return int(value) / 10
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def seconds_value(flat: Mapping[str, Any], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _store(coordinator) -> dict[tuple[str, str], dict[str, Any]]:
    store = getattr(coordinator, "_oven_command_options", None)
    if store is None:
        store = {}
        setattr(coordinator, "_oven_command_options", store)
    return store


def option_key(said: str, cavity: str | None) -> tuple[str, str]:
    return (str(said), str(cavity or "upper"))


def local_options(coordinator, said: str, cavity: str | None) -> dict[str, Any]:
    return _store(coordinator).setdefault(option_key(said, cavity), {})


def update_local_options(coordinator, said: str, cavity: str | None, **updates: Any) -> dict[str, Any]:
    options = local_options(coordinator, said, cavity)
    for key, value in updates.items():
        if value is None:
            options.pop(key, None)
        else:
            options[key] = value
    return options


def current_oven_options(coordinator, said: str, cavity: str | None, flat: Mapping[str, Any]) -> dict[str, Any]:
    """Return full oven options with local idle selections overriding status."""
    prefix = cavity_prefix(cavity)
    local = local_options(coordinator, said, cavity)

    mode_code = str(attr_value(flat, f"{prefix}_CycleSetCommonMode") or "")
    mode = OVEN_MODE_CODE_TO_SERVICE.get(mode_code, "bake")
    target_temp = temp_from_tenths(attr_value(flat, f"{prefix}_CycleSetTargetTemp")) or 176.6

    action_code = str(attr_value(flat, f"{prefix}_OpSetCookTimeCompleteAction") or "3")
    complete_action = OVEN_COMPLETE_ACTION_CODE_TO_SERVICE.get(action_code, "turn_off")

    options: dict[str, Any] = {
        "mode": mode,
        "target_temp": target_temp,
        "cook_time_seconds": seconds_value(flat, f"{prefix}_TimeSetCookTimeSet"),
        "delay_time_seconds": seconds_value(flat, f"{prefix}_TimeSetDelayTime"),
        "complete_action": complete_action,
        "frozen_food": None,
    }
    options.update(local)
    return options
