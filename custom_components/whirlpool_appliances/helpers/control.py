"""Shared control helpers for DDM-backed Whirlpool cooking controls."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.exceptions import ServiceValidationError

from ..const import DOMAIN
from ..entity import attr_value


OVEN_MODE_SERVICE_TO_CODE = {
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

OVEN_COMPLETE_ACTION_SERVICE_TO_CODE = {
    "stay_on": "1",
    "stayon": "1",
    "keep_warm": "2",
    "keepwarm": "2",
    "turn_off": "3",
    "turnoff": "3",
}

CLEAN_MODE_NAME_TO_CODE = {
    "None": "0",
    "Standard": "1",
    "Mid": "2",
    "Low": "3",
    "Steam": "7",
}
CLEAN_MODE_CODE_TO_NAME = {value: key for key, value in CLEAN_MODE_NAME_TO_CODE.items()}

DISPLAY_LANGUAGE_NAME_TO_CODE = {
    "English": "0",
    "French": "1",
    "Spanish": "2",
}
DISPLAY_LANGUAGE_CODE_TO_NAME = {value: key for key, value in DISPLAY_LANGUAGE_NAME_TO_CODE.items()}

TEMPERATURE_UNITS_NAME_TO_CODE = {
    "Fahrenheit": "0",
    "Celsius": "1",
}
TEMPERATURE_UNITS_CODE_TO_NAME = {value: key for key, value in TEMPERATURE_UNITS_NAME_TO_CODE.items()}

TONE_VOLUME_NAME_TO_CODE = {
    "Off": "0",
    "Low": "1",
    "Medium": "2",
    "High": "3",
}
TONE_VOLUME_CODE_TO_NAME = {value: key for key, value in TONE_VOLUME_NAME_TO_CODE.items()}

MWO_MODE_OPTIONS = [
    "Cook",
    "Defrost",
    "Reheat",
    "Popcorn",
    "Soften",
    "Melt",
    "Steam Cook",
    "Auto Cook",
    "Speed Cook Vegetables",
    "Boil And Simmer",
    "Keep Warm",
]
MWO_MODE_TO_ATTR = {
    "Cook": "Mwo_ModeSetCook",
    "Defrost": "Mwo_ModeSetDefrost",
    "Reheat": "Mwo_ModeSetReheat",
    "Popcorn": "Mwo_ModeSetPopcorn",
    "Soften": "Mwo_ModeSetSoften",
    "Melt": "Mwo_ModeSetMelt",
    "Steam Cook": "Mwo_ModeSetSteamCook",
    "Auto Cook": "Mwo_ModeSetAutoCook",
    "Speed Cook Vegetables": "Mwo_ModeSetSpeedCookVegetables",
    "Boil And Simmer": "Mwo_ModeSetBoilAndSimmer",
    "Keep Warm": "Mwo_ModeSetKeepWarm",
}
MWO_PRESETS_BY_MODE = {
    "Cook": {"Manual": "1"},
    "Defrost": {"Manual": "1", "Meat": "2", "Poultry": "3", "Fish": "4", "Bread": "5", "Juice": "6"},
    "Reheat": {"Manual": "1", "Sauce": "2", "Soup": "3", "Pizza Slices": "4", "Casserole": "5", "Dinner Plate": "6", "Beverage": "7"},
    "Popcorn": {"Automatic": "1"},
    "Soften": {"Butter": "1", "Margarine": "2", "Ice Cream": "3", "Cream Cheese": "4"},
    "Melt": {"Manual": "1", "Butter": "2", "Margarine": "3", "Chocolate": "4", "Cheese": "5", "Marshmallows": "6"},
    "Steam Cook": {"Manual": "1", "Steam Potatoes": "2", "Fresh Vegetables": "3", "Frozen Vegetables": "4", "Shrimp": "6"},
    "Auto Cook": {"Rice": "11", "Hot Cereal": "5", "Scrambled Eggs": "6"},
    "Speed Cook Vegetables": {"Baked Potatoes": "1"},
    "Boil And Simmer": {"Manual": "1", "Dry Spaghetti": "4", "Dry Macaroni": "5", "Dry Penne": "6", "Dry Fettuccine": "7"},
    "Keep Warm": {"Manual": "1"},
}
MWO_DONENESS_NAME_TO_CODE = {
    "Default": "0",
    "Less": "1",
    "Normal": "2",
    "More": "3",
}
MWO_DONENESS_CODE_TO_NAME = {value: key for key, value in MWO_DONENESS_NAME_TO_CODE.items()}


def boolish(raw: Any) -> bool | None:
    """Convert Whirlpool boolean-ish values to bool."""
    if raw is None:
        return None
    if isinstance(raw, str):
        normalized = raw.strip().lower()
        if normalized in {"1", "true", "on", "yes", "enabled", "locked", "open"}:
            return True
        if normalized in {"0", "false", "off", "no", "disabled", "unlocked", "closed", ""}:
            return False
    return bool(raw)


def cavity_prefix(cavity: str | None = None) -> str:
    """Return the Whirlpool legacy cavity prefix."""
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def oven_is_active(flat: Mapping[str, Any], cavity: str | None = None) -> bool:
    """Return true when an oven cavity is preheating/cooking."""
    return str(attr_value(flat, f"{cavity_prefix(cavity)}_OpStatusState") or "") in {"1", "2"}


def microwave_is_active(flat: Mapping[str, Any]) -> bool:
    """Return true when the microwave appears active."""
    return str(attr_value(flat, "Mwo_OperationStatusState") or "") in {"1", "2", "3", "6", "8", "9", "10"}


def control_lock_on(flat: Mapping[str, Any]) -> bool:
    return boolish(attr_value(flat, "Sys_OperationSetControlLock")) is True


def remote_control_off(flat: Mapping[str, Any]) -> bool:
    raw = attr_value(flat, "XCat_RemoteSetRemoteControlEnable")
    # Missing values should not block commands on appliance families that do not
    # expose this flag. An explicit 0/false should block with a useful message.
    return raw is not None and boolish(raw) is False


def oven_door_open(flat: Mapping[str, Any], cavity: str | None = None) -> bool:
    return boolish(attr_value(flat, f"{cavity_prefix(cavity)}_OpStatusDoorOpen")) is True


def microwave_door_open(flat: Mapping[str, Any]) -> bool:
    return boolish(attr_value(flat, "Mwo_OperationStatusDoorOpen")) is True


def frozen_or_custom_cycle(flat: Mapping[str, Any], cavity: str | None = None) -> bool:
    prefix = cavity_prefix(cavity)
    frozen = str(attr_value(flat, f"{prefix}_CycleSetFrozenBakeFood") or "0")
    custom = str(attr_value(flat, f"{prefix}_CustomCycleSetId") or "0")
    return frozen not in {"", "0", "None", "none"} or custom not in {"", "0", "None", "none"}


def raise_if_common_blocked(flat: Mapping[str, Any], *, microwave: bool = False, cavity: str | None = None) -> None:
    """Raise Home Assistant translated errors for common command-blocking states."""
    if control_lock_on(flat):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="control_lock_on")
    if microwave and microwave_door_open(flat):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="door_open")
    if not microwave and oven_door_open(flat, cavity):
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="door_open")


def oven_cook_attrs(
    *,
    cavity: str | None = None,
    temperature: float,
    mode: str,
    cook_time_seconds: int | None = None,
    delay_time_seconds: int | None = None,
    complete_action: str = "turn_off",
    operation: str = "2",
) -> dict[str, str]:
    """Build a legacy oven start/modify payload."""
    prefix = cavity_prefix(cavity)
    selected = OVEN_MODE_SERVICE_TO_CODE.get(str(mode).lower())
    if selected is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")
    complete = OVEN_COMPLETE_ACTION_SERVICE_TO_CODE.get(str(complete_action or "turn_off").lower())
    if complete is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")

    attrs: dict[str, str] = {
        f"{prefix}_CycleSetCommonMode": selected,
        f"{prefix}_CycleSetTargetTemp": str(int(round(float(temperature) * 10))),
        f"{prefix}_OpSetCookTimeCompleteAction": complete,
        f"{prefix}_OpSetOperations": operation,
    }
    if cook_time_seconds is not None and int(cook_time_seconds) > 0:
        attrs[f"{prefix}_TimeSetCookTimeSet"] = str(int(cook_time_seconds))
    if delay_time_seconds is not None and int(delay_time_seconds) > 0:
        attrs[f"{prefix}_TimeSetDelayTime"] = str(int(delay_time_seconds))
    return attrs


def microwave_attrs(options: Mapping[str, Any]) -> dict[str, str]:
    """Build microwave SetOnDisplay attributes from local pending options."""
    mode = str(options.get("mode") or "Cook")
    preset = str(options.get("preset") or "Manual")
    attr = MWO_MODE_TO_ATTR.get(mode)
    presets = MWO_PRESETS_BY_MODE.get(mode, {})
    code = presets.get(preset) or next(iter(presets.values()), "1")
    if not attr:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_value_set")

    attrs: dict[str, str] = {
        attr: str(code),
        # User-confirmed value for this combo model: SetOnDisplay, not Start.
        "Mwo_OperationSetOperations": "3",
    }
    cook_time = options.get("cook_time_seconds")
    if cook_time is not None and int(cook_time) > 0:
        attrs["Mwo_TimeSetCookTimeSet"] = str(int(cook_time))
    cook_power = options.get("cook_power")
    if cook_power is not None and int(cook_power) > 0:
        attrs["Mwo_CycleSetCookPower"] = str(int(cook_power))
    amount = options.get("amount")
    if amount is not None and int(amount) > 0:
        attrs["Mwo_CycleSetAmount"] = str(int(amount))
    target_temp = options.get("target_temperature")
    if target_temp is not None and float(target_temp) > 0:
        attrs["Mwo_CycleSetTargetTemp"] = str(int(round(float(target_temp) * 10)))
    doneness = options.get("doneness")
    if doneness and doneness != "Default":
        attrs["Mwo_CycleSetDoneness"] = MWO_DONENESS_NAME_TO_CODE.get(str(doneness), "0")
    return attrs


def local_store(coordinator, attr_name: str) -> dict[tuple[str, str], dict[str, Any]]:
    store = getattr(coordinator, attr_name, None)
    if store is None:
        store = {}
        setattr(coordinator, attr_name, store)
    return store


def microwave_local_options(coordinator, said: str) -> dict[str, Any]:
    store = local_store(coordinator, "_microwave_command_options")
    return store.setdefault(
        (str(said), "microwave"),
        {
            "mode": "Cook",
            "preset": "Manual",
            "cook_time_seconds": 60,
            "cook_power": 100,
            "amount": None,
            "target_temperature": None,
            "doneness": "Default",
        },
    )


def update_microwave_options(coordinator, said: str, **updates: Any) -> dict[str, Any]:
    options = microwave_local_options(coordinator, said)
    for key, value in updates.items():
        options[key] = value
    return options
