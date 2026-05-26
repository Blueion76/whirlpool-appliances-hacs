"""Number entities for Whirlpool setpoints."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import UnitOfTime, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import WhirlpoolApkConfigEntry
from .api import appliance_said
from .entity import WhirlpoolApkEntity, attr_value, celsius_to_unit, entity_name_from_key, find_key, is_cooking_appliance, oven_cavity_exists, unit_to_celsius


def _temp_from_tenths(value) -> float | None:
    if value in (None, "", "0", 0):
        return None
    try:
        return int(value) / 10
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


def _fahrenheit_to_celsius(value: float) -> float:
    return (float(value) - 32) * 5 / 9


def _allowed_oven_temperatures(unit: UnitOfTemperature) -> tuple[float, ...]:
    temps_f = tuple(range(175, 551, 5))
    if unit == UnitOfTemperature.FAHRENHEIT:
        return tuple(float(v) for v in temps_f)
    return tuple(round(_fahrenheit_to_celsius(v), 1) for v in temps_f)


def _snap_oven_temperature(value: float, unit: UnitOfTemperature) -> float:
    allowed = _allowed_oven_temperatures(unit)
    return min(allowed, key=lambda allowed_value: abs(allowed_value - float(value)))


_OVEN_MODE_CODE_TO_SERVICE_MODE = {
    "2": "bake",
    "6": "convect_bake",
    "8": "broil",
    "9": "convect_broil",
    "16": "convect_roast",
    "24": "keep_warm",
    "41": "air_fry",
}


def _cavity_prefix(cavity: str | None) -> str:
    return "OvenLowerCavity" if cavity == "lower" else "OvenUpperCavity"


def _timer_seconds(flat: Mapping[str, object]) -> int | None:
    raw = attr_value(flat, "KitchenTimer01_SetTimeSet")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _seconds_value(flat: Mapping[str, object], attr: str) -> int | None:
    raw = attr_value(flat, attr)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def async_setup_entry(hass: HomeAssistant, entry: WhirlpoolApkConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator = entry.runtime_data
    entities = []
    for appliance in (coordinator.data or {}).get("appliances", []):
        if not appliance_said(appliance):
            continue
        if is_cooking_appliance(appliance):
            flat = WhirlpoolApkEntity(coordinator, appliance, "_probe").flat_status
            if oven_cavity_exists(flat, "upper"):
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "upper", "cook_time"))
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "upper", "delay_time"))
            if oven_cavity_exists(flat, "lower"):
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "lower", "cook_time"))
                entities.append(WhirlpoolOvenTimeNumber(coordinator, appliance, "lower", "delay_time"))
            entities.append(WhirlpoolKitchenTimerNumber(coordinator, appliance))
            continue
        # Phase 7 safety guard: do not expose generic writable setpoints for
        # appliances we cannot test. Read-only temperature sensors are still
        # created by sensor.py; writable controls should be added per category
        # only after DDM/captures confirm the command payload.
        continue
    async_add_entities(entities)


class WhirlpoolTargetTemperatureNumber(WhirlpoolApkEntity, NumberEntity):
    # Enabled by default: this is the preferred oven temperature control. For ovens, setting this value starts/modifies Bake unless another mode is already selected.

    def __init__(self, coordinator, appliance: Mapping[str, object], cavity: str | None) -> None:
        self.cavity = cavity
        suffix = f"{cavity}_target_temperature_setpoint" if cavity else "target_temperature_setpoint"
        super().__init__(coordinator, appliance, suffix)
        unit = self.temperature_unit
        self.entity_description = NumberEntityDescription(
            key=suffix,
            translation_key=suffix,
            native_min_value=_allowed_oven_temperatures(unit)[0],
            native_max_value=_allowed_oven_temperatures(unit)[-1],
            native_step=5,
            native_unit_of_measurement=unit,
            mode=NumberMode.BOX,
        )
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> float | None:
        if self.cavity == "upper":
            value = _temp_from_tenths(attr_value(self.flat_status, "OvenUpperCavity_CycleSetTargetTemp"))
        elif self.cavity == "lower":
            value = _temp_from_tenths(attr_value(self.flat_status, "OvenLowerCavity_CycleSetTargetTemp"))
        else:
            raw = find_key(self.flat_status, ("targetTemperature", "targetTemp", "setTemperature"))
            try:
                value = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                value = None
        if value is None:
            return None

        display_value = celsius_to_unit(value, self.temperature_unit)
        # Whirlpool legacy ovens store/report setpoints in Celsius even for US
        # appliances. Some models round or truncate the returned Celsius value,
        # so a user-entered 300°F may come back as 148°C, which HA would display
        # as 298.4°F. For the user-facing target-temperature number, snap the
        # reported value back to the nearest valid oven setpoint so the control
        # stays on familiar 5°F increments.
        if self.cavity in ("upper", "lower") and display_value is not None:
            return _snap_oven_temperature(display_value, self.temperature_unit)
        return display_value

    async def async_set_native_value(self, value: float) -> None:
        snapped = _snap_oven_temperature(value, self.temperature_unit)
        celsius = unit_to_celsius(snapped, self.temperature_unit)

        # Match Whirlpool Android app behavior: send exact converted Celsius
        # value and let the command payload truncate to tenths.
        # Legacy Minerva cooking appliances ignore a bare
        # ``CycleSetTargetTemp`` write for many oven states. Since this
        # integration no longer exposes an oven climate entity, the number
        # entity is now the primary oven setpoint control. For oven cavities,
        # always send the full cook command payload:
        #
        #   mode + target temp + OpSetOperations=2
        #
        # This starts the oven when it is off and modifies the cook cycle when
        # it is already running. If Whirlpool already reports a selected mode,
        # preserve it; otherwise default to Bake.
        if self.cavity in ("upper", "lower"):
            prefix = _cavity_prefix(self.cavity)
            mode_code = str(attr_value(self.flat_status, f"{prefix}_CycleSetCommonMode") or "")
            mode = _OVEN_MODE_CODE_TO_SERVICE_MODE.get(mode_code, "bake")
            self._check_service_request(
                await self.client.set_oven_cook(self.said, celsius, mode, self.cavity)
            )
        else:
            self._check_service_request(
                await self.client.set_target_temperature(self.said, celsius, self.cavity)
            )

        await self.coordinator.async_request_refresh()



class WhirlpoolOvenTimeNumber(WhirlpoolApkEntity, NumberEntity):
    """Oven cook-time and delay-time number entities."""

    def __init__(self, coordinator, appliance: Mapping[str, object], cavity: str, kind: str) -> None:
        self.cavity = cavity
        self.kind = kind
        suffix = f"{cavity}_oven_{kind}"
        super().__init__(coordinator, appliance, suffix)
        self.entity_description = NumberEntityDescription(
            key=suffix,
            translation_key=suffix,
            icon="mdi:timer" if kind == "cook_time" else "mdi:timer-outline",
            native_min_value=0,
            native_max_value=43140,
            native_step=60,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            mode=NumberMode.BOX,
        )
        self._attr_name = entity_name_from_key(suffix, appliance)

    @property
    def native_value(self) -> int | None:
        prefix = _cavity_prefix(self.cavity)
        attr = f"{prefix}_TimeSetCookTimeSet" if self.kind == "cook_time" else f"{prefix}_TimeSetDelayTime"
        return _seconds_value(self.flat_status, attr)

    async def async_set_native_value(self, value: float) -> None:
        prefix = _cavity_prefix(self.cavity)
        attrs = {
            (f"{prefix}_TimeSetCookTimeSet" if self.kind == "cook_time" else f"{prefix}_TimeSetDelayTime"): str(max(0, int(value)))
        }
        self._check_service_request(await self.client.send_attributes(self.said, attrs))
        await self.coordinator.async_request_refresh()



class WhirlpoolKitchenTimerNumber(WhirlpoolApkEntity, NumberEntity):
    """Typed seconds input for the Whirlpool on-screen kitchen timer."""

    def __init__(self, coordinator, appliance: Mapping[str, object]) -> None:
        super().__init__(coordinator, appliance, "kitchen_timer_1_set")
        self.entity_description = NumberEntityDescription(
            key="kitchen_timer_1_set",
            translation_key="kitchen_timer_1_set",
            icon="mdi:timer",
            native_min_value=0,
            native_max_value=23 * 60 * 60 + 59 * 60 + 59,
            native_step=1,
            native_unit_of_measurement=UnitOfTime.SECONDS,
            mode=NumberMode.BOX,
        )
        self._attr_name = entity_name_from_key("kitchen_timer_1_set", appliance)

    @property
    def native_value(self) -> int | None:
        return _timer_seconds(self.flat_status)

    async def async_set_native_value(self, value: float) -> None:
        self._check_service_request(await self.client.set_kitchen_timer(self.said, int(value), 1))
        await self.coordinator.async_request_refresh()
