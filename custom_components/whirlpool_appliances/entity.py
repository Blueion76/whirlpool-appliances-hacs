"""Entity helpers for Whirlpool Appliances integration."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import appliance_ddm_key, appliance_name, appliance_said
from .const import DOMAIN
from .coordinator import WhirlpoolApkCoordinator

_LOGGER = logging.getLogger(__name__)


def temperature_unit_for_region(region: str | None) -> UnitOfTemperature:
    """Return the HA display temperature unit for a Whirlpool account region.

    Whirlpool legacy cooking attributes report temperatures in tenths of a
    degree Celsius. Home Assistant entities should still display in the
    user-selected Whirlpool region's familiar unit: Fahrenheit for US/NAR,
    Celsius everywhere else.
    """
    return UnitOfTemperature.FAHRENHEIT if str(region or "").upper() == "US" else UnitOfTemperature.CELSIUS


def celsius_to_unit(value: float | int | None, unit: UnitOfTemperature) -> float | None:
    """Convert a Celsius value to the requested HA display unit."""
    if value is None:
        return None
    value_f = float(value)
    if unit == UnitOfTemperature.FAHRENHEIT:
        return round(value_f * 9 / 5 + 32, 1)
    return value_f


def unit_to_celsius(value: float | int | None, unit: UnitOfTemperature) -> float | None:
    """Convert a HA display-unit temperature back to Celsius for Whirlpool."""
    if value is None:
        return None
    value_f = float(value)
    if unit == UnitOfTemperature.FAHRENHEIT:
        return (value_f - 32) * 5 / 9
    return value_f


def first_value(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any | None:
    """Return the first non-empty value from a mapping using case-sensitive keys."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten nested status data for robust entity extraction."""
    out: dict[str, Any] = {}
    if isinstance(data, Mapping):
        for key, value in data.items():
            new_key = f"{prefix}.{key}" if prefix else str(key)
            out.update(flatten(value, new_key))
    elif isinstance(data, list):
        for idx, value in enumerate(data):
            out.update(flatten(value, f"{prefix}.{idx}"))
    else:
        out[prefix] = data
    return out


def find_key(flat: Mapping[str, Any], candidates: tuple[str, ...]) -> Any | None:
    """Find a flattened value by exact or suffix key match."""
    lowered = {k.lower(): v for k, v in flat.items()}
    for candidate in candidates:
        c = candidate.lower()
        if c in lowered:
            return lowered[c]
    for key, value in lowered.items():
        for candidate in candidates:
            c = candidate.lower()
            if key.endswith("." + c) or key.endswith(c):
                return value
    return None


def attr_value(flat: Mapping[str, Any], attr: str) -> Any | None:
    """Return Whirlpool legacy attribute value from /api/v1/appliance/{said}.

    Legacy Whirlpool endpoints return attributes as:
    {"attributes": {"Some_Attr": {"value": "1", "updateTime": ...}}}
    The generic flatten() helper turns that into
    attributes.Some_Attr.value, so this helper checks that shape first and
    then falls back to the raw attribute name for websocket deltas.
    """
    return find_key(
        flat,
        (
            f"attributes.{attr}.value",
            f"attributeMap.{attr}",
            f"{attr}.value",
            attr,
        ),
    )



def is_oven_microwave_combo(appliance: Mapping[str, Any]) -> bool:
    """Return true for wall oven/microwave combo cooking appliances.

    Whirlpool legacy Minerva combo units expose the actual oven as
    ``OvenUpperCavity_*`` and the microwave as ``Mwo_*``. In Home Assistant,
    showing these as "Upper" is confusing because there is no lower oven
    cavity. Use "Oven" for the upper namespace on combo units.
    """
    haystack = appliance_haystack(appliance)
    return "combo" in haystack or "microwave" in haystack or "mwo" in haystack


def entity_key_for_appliance(key: str | None, appliance: Mapping[str, Any] | None = None) -> str | None:
    """Return the display key that should be used for an appliance entity.

    Oven/microwave combo models expose their real oven as ``upper`` in the
    Whirlpool API, but there is no lower oven. For those, keep the existing
    user-friendly ``Oven`` wording. True double ovens should be labeled
    ``Upper Oven`` and ``Lower Oven`` instead of ``Upper Cavity``/``Lower
    Cavity`` or generic ``Upper``/``Lower``.
    """
    if key is None:
        return None
    key = str(key)

    if appliance is not None and is_oven_microwave_combo(appliance):
        if key == "stop_upper_oven":
            return "stop_oven"
        if key.startswith("upper_"):
            return "oven_" + key.removeprefix("upper_")
        return key

    # For true double ovens, make every upper/lower entity clearly refer to an
    # oven. Replace ``*_cavity_*`` with ``*_oven_*`` so names become e.g.
    # "Upper Oven State" and "Lower Oven Light" rather than "Upper Cavity
    # State" / "Lower Cavity Light".
    if key.startswith("upper_cavity_"):
        return "upper_oven_" + key.removeprefix("upper_cavity_")
    if key.startswith("lower_cavity_"):
        return "lower_oven_" + key.removeprefix("lower_cavity_")
    if key.startswith("upper_") and not key.startswith("upper_oven_"):
        return "upper_oven_" + key.removeprefix("upper_")
    if key.startswith("lower_") and not key.startswith("lower_oven_"):
        return "lower_oven_" + key.removeprefix("lower_")

    return key


def entity_name_from_key(key: str | None, appliance: Mapping[str, Any] | None = None) -> str | None:
    """Return a stable human-readable entity name for Home Assistant.

    We still set translation keys where possible, but setting explicit names avoids
    a Home Assistant edge case where _attr_name = None makes every entity inherit
    only the device name (for example, all entities named just "Kitchen").
    """
    key = entity_key_for_appliance(key, appliance)
    if not key:
        return None
    special = {
        "ac": "AC",
        "api": "API",
        "iot": "IoT",
        "mqtt": "MQTT",
        "ts": "TS",
    }
    words = []
    for part in str(key).replace("-", "_").split("_"):
        if not part:
            continue
        lower = part.lower()
        words.append(special.get(lower, lower.capitalize()))
    return " ".join(words) or None


def appliance_haystack(appliance: Mapping[str, Any]) -> str:
    """Return a lowercase search string with Whirlpool model/category hints."""
    return " ".join(
        str(appliance.get(key, ""))
        for key in (
            "CATEGORY_NAME",
            "category",
            "Category",
            "DATA_MODEL_KEY",
            "DATA_MODEL",
            "dataModel",
            "model",
            "thingTypeName",
            "modelNumber",
            "MODEL_NO",
        )
    ).lower()


def is_washer_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for Whirlpool washer data models."""
    haystack = appliance_haystack(appliance)
    return ("washer" in haystack or "laundry" in haystack) and "dryer" not in haystack


def is_dryer_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for Whirlpool dryer data models."""
    return "dryer" in appliance_haystack(appliance)


def is_laundry_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for washer/dryer/laundry data models."""
    haystack = appliance_haystack(appliance)
    return any(term in haystack for term in ("washer", "dryer", "laundry"))


def is_refrigerator_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for Whirlpool refrigerator data models."""
    haystack = appliance_haystack(appliance)
    return any(term in haystack for term in ("refrigerator", "fridge", "ted_refrigerator"))


def is_freezer_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for standalone freezer or freezer-capable refrigeration models."""
    haystack = appliance_haystack(appliance)
    return "freezer" in haystack


def is_refrigeration_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for refrigerators/freezers."""
    return is_refrigerator_appliance(appliance) or is_freezer_appliance(appliance)


def is_aircon_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for Whirlpool air conditioner data models."""
    haystack = appliance_haystack(appliance)
    return any(term in haystack for term in ("airconditioner", "air conditioner", "aircon", "ac_"))


def is_dishwasher_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for Whirlpool dishwasher data models."""
    haystack = appliance_haystack(appliance)
    return any(term in haystack for term in ("dishwasher", "dish_washer", "dish washer", "dishstatus"))


def is_cooktop_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for Whirlpool cooktop data models."""
    haystack = appliance_haystack(appliance)
    return "cooktop" in haystack or "cook_top" in haystack


def has_legacy_attr(flat: Mapping[str, Any], attr: str) -> bool:
    """Return true if a legacy Whirlpool attribute exists in a status payload."""
    needle = f"attributes.{attr}.value".lower()
    attr_lower = attr.lower()
    for key in flat:
        lowered = key.lower()
        if lowered == needle or lowered.endswith("." + needle) or lowered == attr_lower or lowered.endswith("." + attr_lower):
            return True
    return False


def _has_real_status_payload(flat: Mapping[str, Any]) -> bool:
    """Return true when a REST/MQTT payload has real appliance data.

    At platform setup time Home Assistant may call this before the first useful
    refresh finishes. In that case we keep broad entities around. Once the
    payload contains an attributes map, absence of a cavity prefix means that
    cavity is actually not present. This prevents combo microwave/oven models
    from creating fake lower-oven entities.
    """
    return any(
        key.lower().startswith(("status.", "attributes.")) or ".attributes." in key.lower()
        for key in flat
    )


def _has_substantive_oven_cavity_attrs(flat: Mapping[str, Any], prefix: str) -> bool:
    """Return true when a cavity has real cook/display attributes.

    Some combo oven/microwave models expose a stray lower-cavity state when
    Sabbath mode is toggled, even though the appliance has no physical lower
    oven. Do not treat ``OpStatusState`` by itself as proof that a cavity
    exists; require at least one cook/display/control attribute that real oven
    cavities normally expose.
    """
    markers = (
        f"{prefix}_CycleSetTargetTemp",
        f"{prefix}_CycleSetCommonMode",
        f"{prefix}_DisplStatusDisplayTemp",
        f"{prefix}_DisplaySetLightOn",
        f"{prefix}_OpSetOperations",
        f"{prefix}_DoorStatusState",
    )
    return any(attr_value(flat, marker) is not None for marker in markers)


def oven_cavity_exists(flat: Mapping[str, Any], cavity: str) -> bool:
    """Official-style oven cavity existence check.

    State 4 means NotPresent. Other state values only prove a cavity exists when
    paired with real cook/display/control attributes. This prevents combo
    oven/microwave units from creating fake lower-oven entities after Sabbath
    mode exposes lower-cavity status-only keys.
    """
    is_lower = str(cavity).lower().startswith("lower")
    prefix = "OvenLowerCavity" if is_lower else "OvenUpperCavity"
    state = attr_value(flat, f"{prefix}_OpStatusState")
    has_real_cavity_attrs = _has_substantive_oven_cavity_attrs(flat, prefix)

    if state is not None and str(state) == "4":
        return False

    # Combo oven/microwave appliances can report lower-cavity Sabbath/state
    # placeholders. If the Mwo namespace exists and the lower cavity has no
    # actual oven cook/display/control attributes, do not create lower-oven
    # entities.
    has_microwave_namespace = any("mwo_" in key.lower() for key in flat)
    if is_lower and has_microwave_namespace and not has_real_cavity_attrs:
        return False

    if has_real_cavity_attrs:
        return True

    has_prefix = any(prefix.lower() in key.lower() for key in flat)
    if has_prefix:
        # A prefix with only state/Sabbath placeholders is not enough once we
        # have a real status payload.
        return not _has_real_status_payload(flat)

    if _has_real_status_payload(flat):
        return False
    # No useful payload yet. Keep one setup pass permissive so later refreshes
    # can populate entities on appliances that do not return attributes instantly.
    return True


def microwave_exists(flat: Mapping[str, Any]) -> bool:
    """Return true when a Whirlpool combo appliance exposes the Mwo namespace."""
    return any("mwo_" in key.lower() for key in flat)


def is_cooking_appliance(appliance: Mapping[str, Any]) -> bool:
    """Return true for Whirlpool cooking/oven/microwave data models."""
    haystack = appliance_haystack(appliance)
    return any(term in haystack for term in ("cooking", "oven", "minerva", "microwave", "mwo"))


def _has_substantive_status(status: Any) -> bool:
    if not isinstance(status, Mapping):
        return bool(status)
    metadata_keys = {
        "online",
        "source",
        "pending",
        "detail",
        "mqttTopic",
        "mqttRaw",
        "topicModel",
        "lastResponse",
        "lastRequestId",
        "error",
    }
    return any(key not in metadata_keys for key in status)


def appliance_metadata_field(
    appliance: Mapping[str, Any],
    status: Mapping[str, Any] | None,
    keys: tuple[str, ...],
) -> Any | None:
    """Return a metadata value from appliance first, then status."""
    value = first_value(appliance, keys)
    if value not in (None, ""):
        return value
    if isinstance(status, Mapping):
        return first_value(status, keys)
    return None


def appliance_category(appliance: Mapping[str, Any], status: Mapping[str, Any] | None = None) -> str | None:
    """Return normalized category/type metadata for this appliance."""
    value = appliance_metadata_field(
        appliance,
        status,
        ("CATEGORY_NAME", "categoryName", "category", "Category", "applianceCategory", "applianceType", "type"),
    )
    return str(value) if value not in (None, "") else None


class WhirlpoolApkEntity(CoordinatorEntity[WhirlpoolApkCoordinator]):
    """Base entity for one Whirlpool appliance."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WhirlpoolApkCoordinator, appliance: Mapping[str, Any], suffix: str) -> None:
        super().__init__(coordinator)
        self.appliance = dict(appliance)
        self.said = appliance_said(appliance) or "unknown"
        self.entity_suffix = suffix
        self._unavailable_logged = False
        self._attr_unique_id = f"{self.said}_{suffix}"
        initial_status = {}
        model = appliance_metadata_field(
            appliance,
            initial_status,
            ("MODEL_NO", "ModelNumber", "modelNumber", "model_number", "model", "thingTypeName", "DATA_MODEL_KEY", "DATA_MODEL"),
        )
        serial = appliance_metadata_field(appliance, initial_status, ("SERIAL", "SerialNumber", "serialNumber", "serial"))
        ddm_key = appliance_ddm_key(appliance) or first_value(appliance, ("DATA_MODEL_KEY", "dataModelKey"))
        cc_uri = first_value(appliance, ("ccuri", "CC_URI"))
        category = appliance_category(appliance)

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self.said)},
            name=appliance_name(appliance),
            manufacturer=self._manufacturer(appliance),
            model=str(model or ""),
            model_id=str(model or ""),
            serial_number=str(serial or ""),
            hw_version=self.said,
            sw_version=str(cc_uri or ddm_key or ""),
        )
        self._attr_extra_state_attributes = {
            "said": self.said,
            "ddm_key": ddm_key,
            "cc_uri": cc_uri,
            "category": category,
            "model": str(model or "") or None,
            "serial_number": str(serial or "") or None,
        }

    @property
    def client(self):
        return self.coordinator.client

    @property
    def status(self) -> Any:
        return (self.coordinator.data or {}).get("statuses", {}).get(self.said, {})

    @property
    def flat_status(self) -> dict[str, Any]:
        return flatten(self.status)

    @property
    def temperature_unit(self) -> UnitOfTemperature:
        """Return preferred HA display temperature unit for this config entry."""
        region = getattr(self.coordinator.client, "region", None)
        return temperature_unit_for_region(region)

    @property
    def available(self) -> bool:
        """Return entity availability.

        Be conservative about marking entities unavailable. A missing value should
        become an unknown state, not make the whole entity unavailable. Only an
        explicit cloud error or explicit offline/false online marker should make
        it unavailable.
        """
        flat = self.flat_status
        error = find_key(flat, ("error",))
        online = find_key(flat, ("Online", "online", "isOnline", "connected"))
        available = super().available
        if error:
            available = False
        if isinstance(online, bool) and not online:
            available = False
        if isinstance(online, str) and online.lower() in ("0", "false", "offline", "disconnected"):
            available = False
        if not available and not self._unavailable_logged:
            _LOGGER.info("Whirlpool entity %s is unavailable", self._attr_unique_id)
            self._unavailable_logged = True
        elif available and self._unavailable_logged:
            _LOGGER.info("Whirlpool entity %s is back online", self._attr_unique_id)
            self._unavailable_logged = False
        return available

    def _remote_enable_is_off(self) -> bool:
        """Return true if the latest status says remote control is disabled."""
        raw = attr_value(
            self.flat_status,
            "XCat_RemoteSetRemoteControlEnable",
            "XCat_RemoteControlEnable",
            "remoteControlEnable",
            "remoteEnable",
            "remoteEnabled",
        )
        if raw is None:
            return False
        return str(raw).strip().lower() in {"0", "false", "off", "disabled"}

    def _raise_command_failed(self) -> None:
        if self._remote_enable_is_off():
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="remote_enable_off",
            )
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="request_failed",
        )

    def _check_service_request(self, result: Any) -> None:
        """Raise a Home Assistant error when a Whirlpool control request failed."""
        if result is False:
            self._raise_command_failed()
        if isinstance(result, Mapping):
            status = str(result.get("status", "")).strip().lower()
            message = str(result.get("message", "")).strip().lower()
            if status in {"error", "failed", "fail", "02", "2", "nack"} or "negative acknow" in message:
                self._raise_command_failed()

    def _manufacturer(self, appliance: Mapping[str, Any]) -> str:
        model = str(first_value(appliance, ("MODEL_NO", "modelNumber", "model", "model_number")) or "")
        brand = str(first_value(appliance, ("brand", "Brand", "manufacturer", "MANUFACTURER")) or "")
        if brand:
            return brand.title() if brand.isupper() else brand
        if model.startswith("K"):
            return "KitchenAid"
        if model.startswith("M"):
            return "Maytag"
        if model.startswith("J"):
            return "JennAir"
        if model.startswith("A"):
            return "Amana"
        return "Whirlpool"
