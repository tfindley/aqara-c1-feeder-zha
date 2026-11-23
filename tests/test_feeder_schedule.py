"""Tests for the Aqara C1 feeder schedule quirk.

Run against the same library versions Home Assistant ships, e.g.:
    pip install -r tests/requirements.txt pytest pytest-asyncio
    pytest -q tests
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import importlib.util
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest
import zhaquirks
import zigpy.device
import zigpy.types as t
from zigpy.time import TimeSnapshot
from zigpy.zcl import foundation
from zha.quirks import DEVICE_REGISTRY, ModelInfo

QUIRK_DIR = pathlib.Path(__file__).resolve().parent.parent / "custom_zha_quirks"
MODEL = "aqara.feeder.acn001"
IEEE = t.EUI64.convert("00:11:22:33:44:55:66:77")

# Load the stock quirks, then the custom quirk directory, the same way ZHA does.
zhaquirks.setup(custom_quirks_path=str(QUIRK_DIR))
quirk = sys.modules["aqara_feeder_acn001_schedule"]


# ---------------------------------------------------------------- codec ----


@pytest.mark.parametrize(
    ("text", "wire"),
    [
        # Same bytes Zigbee2MQTT sends for {days: everyday, hour: 7, minute: 0, size: 2}
        ("07:00x2", b"7f07000200\x00"),
        ("07:00x2,19:30x1", b"7f07000200,7f131e0100\x00"),
        ("workdays@06:30x2,weekend@08:05x3", b"1f061e0200,6008050300\x00"),
        ("mon+wed@23:59x10", b"05173b0a00\x00"),
        ("85@12:00x1", b"550c000100\x00"),
    ],
)
def test_encode_matches_z2m_wire_format(text: str, wire: bytes) -> None:
    assert quirk.encode_schedule(quirk.parse_schedule(text)) == wire


@pytest.mark.parametrize(
    ("text", "normalized"),
    [
        ("07:00x2", "07:00x2"),
        (" 7:00x2 ; 19:30X1 ", "07:00x2,19:30x1"),
        ("everyday@07:00x2", "07:00x2"),
        ("mon+tue+wed+thu+fri@06:30x2", "workdays@06:30x2"),
        ("mon+wed@23:59x10", "mon+wed@23:59x10"),
        ("85@12:00x1", "mon-wed-fri-sun@12:00x1"),
    ],
)
def test_roundtrip(text: str, normalized: str) -> None:
    entries = quirk.parse_schedule(text)
    assert quirk.format_schedule(entries) == normalized
    assert quirk.decode_schedule(quirk.encode_schedule(entries)) == entries


@pytest.mark.parametrize(
    "bad",
    ["", ",", "7x2", "24:00x1", "12:60x1", "12:00x0", "12:00x11", "funday@12:00x1",
     "0@12:00x1", "128@12:00x1", ",".join(["12:00x1"] * 11)],
)
def test_rejects_invalid(bad: str) -> None:
    with pytest.raises(quirk.ScheduleError):
        quirk.parse_schedule(bad)


def test_decode_tolerates_placeholders_and_garbage() -> None:
    assert quirk.decode_schedule(b"//,7f07000200,zz,\x00junk") == [(127, 7, 0, 2)]
    assert quirk.decode_schedule(b"\x00") == []


# ------------------------------------------------------------- registry ----


def test_custom_quirk_takes_precedence() -> None:
    entries = DEVICE_REGISTRY._registry[ModelInfo(None, MODEL)]
    assert len(entries) >= 2, "stock quirk and custom quirk should both be registered"
    assert "aqara_feeder_acn001_schedule" in repr(entries[0].source)


def _make_feeder() -> zigpy.device.Device:
    app = MagicMock()
    app.get_device.return_value = None
    dev = zigpy.device.Device(app, IEEE, 0x1234)
    dev.model = MODEL
    dev.manufacturer = None
    ep = dev.add_endpoint(1)
    ep.status = 1  # ENDPOINT_INITIALIZED
    ep.profile_id = 0x0104
    ep.device_type = 0x0051
    for cid in (0x0000, 0x0003, 0x0006, 0xFCC0):
        ep.add_input_cluster(cid)
    return dev


def test_device_resolves_to_schedule_cluster() -> None:
    dev = DEVICE_REGISTRY.resolve(_make_feeder())
    cluster = dev.endpoints[1].in_clusters[0xFCC0]
    assert isinstance(cluster, quirk.ScheduleOppleCluster)
    assert 0x0006 not in dev.endpoints[1].in_clusters  # OnOff removed like stock quirk
    assert 0x000A in dev.endpoints[1].in_clusters  # Time added like stock quirk
    assert isinstance(dev.endpoints[1].in_clusters[0x000A], quirk.LocalTimeCluster)


# -------------------------------------------------------------- cluster ----


@pytest.fixture
def cluster():
    dev = DEVICE_REGISTRY.resolve(_make_feeder())
    cl = dev.endpoints[1].in_clusters[0xFCC0]
    cl.write_attributes_raw = AsyncMock(
        return_value=[[foundation.WriteAttributesStatusRecord(foundation.Status.SUCCESS)]]
    )
    return cl


async def test_write_schedule_sends_aqara_frame(cluster) -> None:
    # Exactly what the zha.set_zigbee_cluster_attribute action does.
    result = await cluster.write_attributes(
        {quirk.ZCL_FEEDING_SCHEDULE: t.CharacterString("07:00x2,11:00x2")},
        manufacturer=0x115F,
    )
    assert result[0][0].status == foundation.Status.SUCCESS

    (records,), kwargs = cluster.write_attributes_raw.call_args
    assert kwargs["manufacturer_code"] == 0x115F
    assert len(records) == 1
    rec = records[0]
    assert rec.attrid == 0xFFF1
    frame = bytes(rec.value.value)
    payload = b"7f07000200,7f0b000200\x00"
    assert frame[0:2] == b"\x00\x02"
    assert frame[3:7] == bytes.fromhex("080008c8")
    assert frame[7] == len(payload)
    assert frame[8:] == payload

    # Optimistically cached after a successful write.
    assert cluster.get("feeding_schedule") == "07:00x2,11:00x2"


async def test_write_schedule_by_name_and_invalid_value(cluster) -> None:
    await cluster.write_attributes({"feeding_schedule": "workdays@06:30x2"})
    assert cluster.get("feeding_schedule") == "workdays@06:30x2"

    with pytest.raises(quirk.ScheduleError):
        await cluster.write_attributes({"feeding_schedule": "25:00x1"})


async def test_failed_write_does_not_update_cache(cluster) -> None:
    cluster.write_attributes_raw = AsyncMock(
        return_value=[[foundation.WriteAttributesStatusRecord(
            foundation.Status.FAILURE, 0xFFF1)]]
    )
    await cluster.write_attributes({"feeding_schedule": "07:00x2"})
    assert cluster.get("feeding_schedule") is None


async def test_other_feeder_attributes_still_work(cluster) -> None:
    await cluster.write_attributes({"feeding_mode": 1}, manufacturer=0x115F)
    (records,), _ = cluster.write_attributes_raw.call_args
    frame = bytes(records[0].value.value)
    assert frame[3:7] == bytes.fromhex("04180055") and frame[8:] == b"\x01"


def test_reported_schedule_updates_sensor(cluster) -> None:
    payload = b"7f07000200,6008000100\x00"
    frame = b"\x00\x05\x11" + bytes.fromhex("080008c8") + bytes([len(payload)]) + payload
    cluster._parse_feeder_attribute(frame)
    assert cluster.get("feeding_schedule") == "07:00x2,weekend@08:00x1"


def test_reported_schedule_echo_layout(cluster) -> None:
    # Layout observed from a real C1 right after a schedule write: two extra bytes
    # (05 a5) before the attribute id, uppercase hex, no NUL terminator.
    payload = b"7F07000200,7F0B000200,7F0F000200,7F13000200,7F17000200"
    frame = b"\x00\x05\x11\x05\xa5" + bytes.fromhex("080008c8") + bytes([len(payload)]) + payload
    cluster._parse_feeder_attribute(frame)
    assert cluster.get("feeding_schedule") == "07:00x2,11:00x2,15:00x2,19:00x2,23:00x2"


def test_reported_schedule_split_over_two_frames(cluster) -> None:
    # Exactly what a real C1 sent: 53 of 54 characters, then a continuation frame.
    full = b"7F07000200,7F0B000200,7F0F000200,7F13000200,7F17000200"
    first = b"\x00\x05\x11\x05\xa5" + bytes.fromhex("080008c8") + bytes([len(full)]) + full[:-1]
    cluster._parse_feeder_attribute(first)
    assert cluster.get("feeding_schedule") is None  # incomplete, not stored yet
    cluster._parse_feeder_attribute(b"\x80\x02\x02" + full[-1:])
    assert cluster.get("feeding_schedule") == "07:00x2,11:00x2,15:00x2,19:00x2,23:00x2"


def test_short_frame_is_ignored(cluster) -> None:
    cluster._parse_feeder_attribute(b"\x00\x05\x11\x05")  # must not raise


def test_other_reports_still_parsed(cluster) -> None:
    frame = b"\x00\x05\x12" + bytes.fromhex("04180055") + b"\x01\x01"
    cluster._parse_feeder_attribute(frame)
    assert cluster.get("feeding_mode") == 1


async def test_read_schedule_answers_from_cache(cluster) -> None:
    cluster.read_attributes_raw = AsyncMock(side_effect=AssertionError("must not hit device"))
    success, failure = await cluster.read_attributes(["feeding_schedule"])
    assert success == {} and failure == {}
    cluster._update_attribute(quirk.ZCL_FEEDING_SCHEDULE, t.CharacterString("07:00x2"))
    success, failure = await cluster.read_attributes(["feeding_schedule"], allow_cache=False)
    assert success == {"feeding_schedule": "07:00x2"} and failure == {}
    assert not cluster.is_attribute_unsupported("feeding_schedule")


def test_schedule_sensor_metadata() -> None:
    entry = DEVICE_REGISTRY._registry[ModelInfo(None, MODEL)][0]
    text = repr(entry)
    assert "feeding_schedule" in text
    assert "0xfcc0-feeding_schedule" in text.lower() or "64704-feeding_schedule" in text


# ---------------------------------------------------------------- clock ----

PARIS = ZoneInfo("Europe/Paris")


@pytest.fixture
def time_cluster():
    dev = DEVICE_REGISTRY.resolve(_make_feeder())
    return dev.endpoints[1].in_clusters[0x000A]


@pytest.mark.parametrize(
    ("utc_now", "offset_hours"),
    [
        (datetime(2026, 11, 23, 22, 0, tzinfo=UTC), 1),  # winter: CET, UTC+1
        (datetime(2026, 7, 1, 21, 0, tzinfo=UTC), 2),  # summer: CEST, UTC+2
    ],
)
def test_time_attribute_is_local_time(time_cluster, utc_now, offset_hours) -> None:
    snapshot = TimeSnapshot.compute(PARIS, utc_now)
    answered = time_cluster.handle_read_attribute_time(snapshot)
    assert int(answered) == int(snapshot.time) + offset_hours * 3600
    assert int(answered) == int(snapshot.local_time)
    # 23:00 local must read back as 23:00 on the feeder's clock.
    assert t.UTCTime(answered).to_datetime().hour == utc_now.astimezone(PARIS).hour


def test_invalid_clock_is_passed_through(time_cluster) -> None:
    snapshot = TimeSnapshot.compute(PARIS, datetime(2026, 11, 23, 22, 0, tzinfo=UTC))
    invalid = replace(snapshot, time=t.UTCTime(t.UTCTime.INVALID))
    assert time_cluster.handle_read_attribute_time(invalid) == t.UTCTime.INVALID


def test_read_request_answers_local_time(time_cluster) -> None:
    # Full path the feeder triggers: a Read Attributes request for time (0x0000).
    time_cluster.endpoint.device.application.local_timezone = PARIS
    before = datetime.now(UTC)
    records = time_cluster._handle_read_attributes([0x0000, 0x0007])
    by_id = {r.attrid: r for r in records}
    assert all(r.status == foundation.Status.SUCCESS for r in records)
    answered = int(by_id[0x0000].value.value)
    assert answered == int(by_id[0x0007].value.value)  # same as local_time
    offset = int(before.astimezone(PARIS).utcoffset().total_seconds())
    utc_now = int(t.UTCTime.from_datetime(before))
    assert abs(answered - (utc_now + offset)) <= 2
