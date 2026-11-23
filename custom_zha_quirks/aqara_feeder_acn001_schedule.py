"""Aqara C1 pet feeder (aqara.feeder.acn001) with an on-device feeding schedule.

Drop-in ZHA custom quirk. It extends the stock zha-quirks v2 quirk for this
feeder (`zhaquirks.xiaomi.aqara.feeder_acn001`) with one extra attribute,
`feeding_schedule`. Writing it stores the schedule *on the feeder itself*, so
the feeder keeps feeding on time when Home Assistant, Wi-Fi or mains power is
down (the feeder runs on its backup batteries).

Schedule format (human readable, used for both writing and the sensor):

    [<days>@]HH:MMx<portions>[,[<days>@]HH:MMx<portions> ...]

    07:00x2,12:00x1,19:30x2       every day
    workdays@06:30x2,weekend@08:00x2
    mon+wed+fri@18:00x1

<days> is optional (default: everyday). Accepted values: everyday, workdays,
weekend, mon, tue, wed, thu, fri, sat, sun, mon-wed-fri-sun, tue-thu-sat, any
combination of day names joined with '+', or a raw bitmask 1-127
(mon=1, tue=2, wed=4, thu=8, fri=16, sat=32, sun=64).

Write it with the `zha.set_zigbee_cluster_attribute` action:

    action: zha.set_zigbee_cluster_attribute
    data:
      ieee: "00:11:22:33:44:55:66:77"
      endpoint_id: 1
      cluster_id: 0xFCC0        # 64704
      cluster_type: in
      attribute: 0x1393         # 5011, "feeding_schedule"
      value: "07:00x2,11:00x2,15:00x2,19:00x2,23:00x2"

The on-wire encoding matches zigbee-herdsman-converters' `lumi_feeder`
converter (Zigbee2MQTT): each entry is the hex string of
[days, hour, minute, portions, 0], entries are joined with ',' and the ASCII
string is NUL-terminated, then sent as Aqara sub-attribute 0x080008C8 inside
manufacturer attribute 0xFFF1 of cluster 0xFCC0.

The quirk also answers the feeder's time requests with local time rather
than UTC. Otherwise the feeder's clock, and so every scheduled feed, is off
by the UTC offset.

SPDX-License-Identifier: Apache-2.0
Derived from zigpy/zha-device-handlers (Apache-2.0).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Final

from zigpy import types
from zigpy.typing import UNDEFINED, UndefinedType
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import OnOff, Time
from zigpy.zcl.foundation import ZCLAttributeDef

from zhaquirks.builder import (
    BinarySensorDeviceClass,
    EntityPlatform,
    EntityType,
    QuirkBuilder,
    SensorStateClass,
    UnitOfMass,
)
from zhaquirks.xiaomi.aqara.feeder_acn001 import (
    FEEDER_ATTR_NAME,
    SCHEDULING_STRING,
    FeedingMode,
    FeedingSource,
    OppleCluster,
)

LOGGER = logging.getLogger(__name__)

# Fake ZCL attribute id for the schedule (the stock quirk uses 0x1388-0x1392).
ZCL_FEEDING_SCHEDULE: Final = 0x1393
MANUFACTURER_CODE: Final = 0x115F

_SCHEDULE_MARKER: Final = SCHEDULING_STRING.to_bytes(4, "big")  # 08 00 08 c8

MAX_ENTRIES: Final = 10
MIN_PORTIONS: Final = 1
MAX_PORTIONS: Final = 10

# ---------------------------------------------------------------------------
# Schedule codec (pure functions, no zigpy needed)
# ---------------------------------------------------------------------------

_DAY_BITS: Final[dict[str, int]] = {
    "mon": 1,
    "tue": 2,
    "wed": 4,
    "thu": 8,
    "fri": 16,
    "sat": 32,
    "sun": 64,
}
# Named day sets, same names Zigbee2MQTT uses.
_DAY_SETS: Final[dict[str, int]] = {
    "everyday": 127,
    "workdays": 31,
    "weekend": 96,
    "mon-wed-fri-sun": 85,
    "tue-thu-sat": 42,
    **_DAY_BITS,
}
_DAY_NAMES_BY_MASK: Final[dict[int, str]] = {v: k for k, v in _DAY_SETS.items()}

_ENTRY_RE: Final = re.compile(
    r"^(?:(?P<days>[a-z0-9+\-]+)@)?(?P<hour>\d{1,2}):(?P<minute>\d{2})x(?P<size>\d{1,2})$"
)


class ScheduleError(ValueError):
    """Invalid feeding schedule."""


def _parse_days(text: str | None) -> int:
    if not text:
        return _DAY_SETS["everyday"]
    if text.isdigit():
        mask = int(text)
    elif text in _DAY_SETS:
        mask = _DAY_SETS[text]
    else:
        mask = 0
        for part in text.split("+"):
            if part not in _DAY_BITS:
                raise ScheduleError(f"unknown day {part!r} in {text!r}")
            mask |= _DAY_BITS[part]
    if not 1 <= mask <= 127:
        raise ScheduleError(f"day mask {mask} out of range 1-127")
    return mask


def _format_days(mask: int) -> str:
    if mask in _DAY_NAMES_BY_MASK:
        return _DAY_NAMES_BY_MASK[mask]
    return "+".join(name for name, bit in _DAY_BITS.items() if mask & bit)


def parse_schedule(text: str) -> list[tuple[int, int, int, int]]:
    """Parse the human schedule format into (days, hour, minute, portions)."""
    cleaned = str(text).strip().lower().replace(" ", "").replace(";", ",")
    if not cleaned:
        raise ScheduleError("schedule is empty")
    entries = []
    for raw in cleaned.split(","):
        if not raw:
            continue
        match = _ENTRY_RE.match(raw)
        if match is None:
            raise ScheduleError(
                f"cannot parse {raw!r}; expected [days@]HH:MMx<portions>, e.g. 07:30x2"
            )
        hour, minute, size = (int(match[k]) for k in ("hour", "minute", "size"))
        if hour > 23 or minute > 59:
            raise ScheduleError(f"invalid time in {raw!r}")
        if not MIN_PORTIONS <= size <= MAX_PORTIONS:
            raise ScheduleError(
                f"portions must be {MIN_PORTIONS}-{MAX_PORTIONS} in {raw!r}"
            )
        entries.append((_parse_days(match["days"]), hour, minute, size))
    if not entries:
        raise ScheduleError("schedule is empty")
    if len(entries) > MAX_ENTRIES:
        raise ScheduleError(f"at most {MAX_ENTRIES} feedings are supported")
    return entries


def format_schedule(entries: list[tuple[int, int, int, int]]) -> str:
    """Format entries back into the human schedule format."""
    parts = []
    for days, hour, minute, size in entries:
        prefix = "" if days == _DAY_SETS["everyday"] else f"{_format_days(days)}@"
        parts.append(f"{prefix}{hour:02d}:{minute:02d}x{size}")
    return ",".join(parts)


def encode_schedule(entries: list[tuple[int, int, int, int]]) -> bytes:
    """Encode entries into the feeder's wire format (NUL-terminated ASCII)."""
    items = [bytes([days, hour, minute, size, 0]).hex() for days, hour, minute, size in entries]
    return ",".join(items).encode("ascii") + b"\x00"


def decode_schedule(payload: bytes) -> list[tuple[int, int, int, int]]:
    """Decode the feeder's wire format into entries."""
    text = payload.split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
    entries = []
    for item in text.split(","):
        item = item.strip()
        if not item or item == "//":
            continue
        try:
            raw = bytes.fromhex(item)
        except ValueError:
            LOGGER.debug("Ignoring malformed schedule item %r", item)
            continue
        if len(raw) >= 4:
            entries.append((raw[0], raw[1], raw[2], raw[3]))
    return entries


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------


class ScheduleOppleCluster(OppleCluster):
    """Aqara feeder cluster with feeding schedule support."""

    class AttributeDefs(OppleCluster.AttributeDefs):
        """Attribute definitions."""

        feeding_schedule: Final = ZCLAttributeDef(
            id=ZCL_FEEDING_SCHEDULE,
            type=types.CharacterString,
            manufacturer_code=MANUFACTURER_CODE,
        )

    def _is_schedule_attr(
        self, attribute: Any, manufacturer: int | UndefinedType | None = UNDEFINED
    ) -> bool:
        try:
            attr_def = self.find_attribute(attribute, manufacturer_code=manufacturer)
        except (KeyError, ValueError):
            return False
        return attr_def.id == ZCL_FEEDING_SCHEDULE

    def is_attribute_unsupported(
        self, attr: int | str | foundation.ZCLAttributeDef
    ) -> bool:
        """The schedule is a quirk-defined attribute and always supported."""
        if self._is_schedule_attr(attr):
            return False
        return super().is_attribute_unsupported(attr)

    async def read_attributes(
        self,
        attributes: list[int | str | foundation.ZCLAttributeDef],
        allow_cache: bool = False,
        only_cache: bool = False,
        manufacturer: int | UndefinedType | None = UNDEFINED,
        **kwargs,
    ) -> Any:
        """Answer the schedule from the cache; the device cannot be read directly."""
        schedule_attrs = [a for a in attributes if self._is_schedule_attr(a, manufacturer)]
        other_attrs = [a for a in attributes if a not in schedule_attrs]

        success: dict[Any, Any] = {}
        failure: dict[Any, Any] = {}
        if other_attrs:
            success, failure = await super().read_attributes(
                other_attrs,
                allow_cache=allow_cache,
                only_cache=only_cache,
                manufacturer=manufacturer,
                **kwargs,
            )
        for attr in schedule_attrs:
            value = self.get(ZCL_FEEDING_SCHEDULE)
            if value is not None:
                success[attr] = value
        return success, failure

    def _parse_feeder_attribute(self, value: bytes) -> None:
        """Also handle the schedule the feeder reports back.

        The feeder reports the schedule in two layouts: the usual
        [3-byte header][attr][len][value], and a variant with two extra bytes
        (05 a5) before the attribute id. The latter is sent right after a
        schedule write, with the stored schedule as uppercase hex.
        """
        value = bytes(value)

        # Long schedules are split over several reports: continuation frames start
        # with a byte that has the 0x80 bit set, followed by two header bytes.
        pending = getattr(self, "_schedule_pending", None)
        if pending is not None and value[:1] and value[0] & 0x80:
            expected, data = pending
            data += value[3:]
            if len(data) >= expected:
                self._schedule_pending = None
                self._store_reported_schedule(data[:expected])
            else:
                self._schedule_pending = (expected, data)
            return

        offset = value.find(_SCHEDULE_MARKER)
        if offset in (3, 5) and len(value) > offset + 4:
            length = value[offset + 4]
            payload = value[offset + 5 : offset + 5 + length]
            if len(payload) < length:
                LOGGER.debug("Schedule report continues in next frame: %r", payload)
                self._schedule_pending = (length, payload)
                return
            self._schedule_pending = None
            self._store_reported_schedule(payload)
            return
        if len(value) < 8:
            LOGGER.debug("Ignoring short feeder frame %r", value)
            return
        super()._parse_feeder_attribute(value)

    def _store_reported_schedule(self, payload: bytes) -> None:
        human = format_schedule(decode_schedule(payload))
        LOGGER.debug("Feeder reported schedule %r -> %r", payload, human)
        self._update_attribute(ZCL_FEEDING_SCHEDULE, types.CharacterString(human))

    async def write_attributes(
        self,
        attributes: dict[str | int | foundation.ZCLAttributeDef, Any],
        **kwargs,
    ) -> list[list[foundation.WriteAttributesStatusRecord]]:
        """Translate a feeding_schedule write into the Aqara schedule frame."""
        attrs: dict[Any, Any] = {}
        schedule_text: str | None = None
        manufacturer = kwargs.get("manufacturer", UNDEFINED)
        for attr, value in attributes.items():
            if self._is_schedule_attr(attr, manufacturer):
                entries = parse_schedule(value)
                schedule_text = format_schedule(entries)
                payload = encode_schedule(entries)
                name, cooked = self._build_feeder_attribute(
                    SCHEDULING_STRING, payload, len(payload)
                )
                attrs[name] = cooked
                LOGGER.debug("Writing schedule %r as %r", schedule_text, cooked)
            else:
                attrs[attr] = value

        result = await super().write_attributes(attrs, **kwargs)

        if schedule_text is not None and _write_succeeded(result):
            # The feeder acknowledged the write; show it until it reports back.
            self._update_attribute(
                ZCL_FEEDING_SCHEDULE, types.CharacterString(schedule_text)
            )
        return result


def _write_succeeded(result: Any) -> bool:
    try:
        records = result[0]
        return all(r.status == foundation.Status.SUCCESS for r in records)
    except (IndexError, TypeError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Clock. The feeder asks the coordinator for the time (ZCL Time cluster) and
# runs its schedule on the `time` attribute, which it treats as *local* time.
# The ZCL spec, and zigpy, put UTC there, so every scheduled feed came late by
# the UTC offset (1 h in winter, 2 h in summer in Central Europe). Answer with
# local wall-clock time instead, like Zigbee2MQTT's customTimeResponse
# ("2000_LOCAL"). zigpy already works out local time, summer time included,
# from Home Assistant's time zone; this only changes which value goes in `time`.
# ---------------------------------------------------------------------------


class LocalTimeCluster(Time):
    """Time cluster that answers the `time` attribute with local time."""

    def handle_read_attribute_time(self, snapshot: Any) -> types.UTCTime:
        if snapshot.time == types.UTCTime.INVALID:
            return snapshot.time
        local = types.UTCTime(int(snapshot.local_time))
        LOGGER.debug(
            "%s read the time: answering local %d instead of UTC %d",
            self.endpoint.device.ieee,
            local,
            snapshot.time,
        )
        return local


# ---------------------------------------------------------------------------
# Quirk definition: identical to the stock quirk plus the schedule sensor and
# the local-time clock, so existing entity IDs are kept.
# ---------------------------------------------------------------------------

(
    QuirkBuilder(None, "aqara.feeder.acn001")
    .friendly_name(manufacturer="Aqara", model="aqara.feeder.acn001")
    .removes(OnOff.cluster_id)
    .adds(LocalTimeCluster)
    .replaces(ScheduleOppleCluster)
    .enum(
        attribute_name=OppleCluster.AttributeDefs.last_feeding_source.name,
        enum_class=FeedingSource,
        cluster_id=OppleCluster.cluster_id,
        entity_platform=EntityPlatform.SENSOR,
        entity_type=EntityType.STANDARD,
        unique_id_suffix=f"{OppleCluster.cluster_id}-last_feeding_source",
        translation_key="last_feeding_source",
        fallback_name="Last feeding source",
    )
    .sensor(
        attribute_name=OppleCluster.AttributeDefs.last_feeding_size.name,
        cluster_id=OppleCluster.cluster_id,
        unique_id_suffix=f"{OppleCluster.cluster_id}-last_feeding_size",
        translation_key="last_feeding_size",
        fallback_name="Last feeding size",
    )
    .sensor(
        attribute_name=OppleCluster.AttributeDefs.portions_dispensed.name,
        cluster_id=OppleCluster.cluster_id,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unique_id_suffix=f"{OppleCluster.cluster_id}-portions_dispensed",
        translation_key="portions_dispensed_today",
        fallback_name="Portions dispensed today",
    )
    .sensor(
        attribute_name=OppleCluster.AttributeDefs.weight_dispensed.name,
        cluster_id=OppleCluster.cluster_id,
        unit=UnitOfMass.GRAMS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        unique_id_suffix=f"{OppleCluster.cluster_id}-weight_dispensed",
        translation_key="weight_dispensed_today",
        fallback_name="Weight dispensed today",
    )
    .switch(
        attribute_name=OppleCluster.AttributeDefs.disable_led_indicator.name,
        cluster_id=OppleCluster.cluster_id,
        force_inverted=True,
        unique_id_suffix=f"{OppleCluster.cluster_id}-disable_led_indicator",
        translation_key="led_indicator",
        fallback_name="LED indicator",
    )
    .switch(
        attribute_name=OppleCluster.AttributeDefs.child_lock.name,
        cluster_id=OppleCluster.cluster_id,
        unique_id_suffix=f"{OppleCluster.cluster_id}-child_lock",
        translation_key="child_lock",
        fallback_name="Child lock",
    )
    .enum(
        attribute_name=OppleCluster.AttributeDefs.feeding_mode.name,
        enum_class=FeedingMode,
        cluster_id=OppleCluster.cluster_id,
        unique_id_suffix=f"{OppleCluster.cluster_id}-feeding_mode",
        translation_key="feeding_mode",
        fallback_name="Feeding mode",
    )
    .number(
        attribute_name=OppleCluster.AttributeDefs.serving_size.name,
        cluster_id=OppleCluster.cluster_id,
        min_value=1,
        max_value=10,
        mode="box",
        unique_id_suffix=f"{OppleCluster.cluster_id}-serving_size",
        translation_key="serving_size",
        fallback_name="Serving size",
    )
    .number(
        attribute_name=OppleCluster.AttributeDefs.portion_weight.name,
        cluster_id=OppleCluster.cluster_id,
        min_value=1,
        max_value=100,
        unit=UnitOfMass.GRAMS,
        mode="box",
        unique_id_suffix=f"{OppleCluster.cluster_id}-portion_weight",
        translation_key="portion_weight",
        fallback_name="Portion weight",
    )
    .binary_sensor(
        attribute_name=OppleCluster.AttributeDefs.error_detected.name,
        cluster_id=OppleCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        device_class=BinarySensorDeviceClass.PROBLEM,
        unique_id_suffix=f"{OppleCluster.cluster_id}-error_detected",
        fallback_name="Error detected",
    )
    .write_attr_button(
        attribute_name=OppleCluster.AttributeDefs.feeding.name,
        attribute_value=1,
        cluster_id=OppleCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        unique_id_suffix=f"{OppleCluster.cluster_id}-feeding",
        translation_key="feed",
        fallback_name="Feed",
    )
    # New: the schedule stored on the feeder.
    .sensor(
        attribute_name=ScheduleOppleCluster.AttributeDefs.feeding_schedule.name,
        cluster_id=OppleCluster.cluster_id,
        entity_type=EntityType.STANDARD,
        attribute_converter=str,
        suggested_display_precision=None,
        unique_id_suffix=f"{OppleCluster.cluster_id}-feeding_schedule",
        # No HA translation exists for this key, so HA shows the fallback name.
        translation_key="feeding_schedule",
        fallback_name="Feeding schedule",
    )
    .add_to_registry()
)
