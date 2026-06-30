"""
Tests for h02_parser.py
Run: pytest tests/test_h02_parser.py -v
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from datetime import timezone
from h02_parser import parse_packet, build_ack, _nmea_to_decimal, _parse_status


# ── NMEA helpers ─────────────────────────────────────────────────────────────

class TestNMEAConversion:
    def test_latitude_north(self):
        # 37°45.9502'N → 37 + 45.9502/60 = 37.76584
        result = _nmea_to_decimal("3745.9502", "N")
        assert result is not None
        assert abs(result - 37.76584) < 0.00001

    def test_latitude_south(self):
        result = _nmea_to_decimal("3745.9502", "S")
        assert result is not None
        assert result < 0
        assert abs(result + 37.76584) < 0.00001

    def test_longitude_east(self):
        # 123°01.1000'E → 123 + 1.1/60 = 123.01833
        result = _nmea_to_decimal("12301.1000", "E")
        assert result is not None
        assert abs(result - 123.01833) < 0.0001

    def test_longitude_west(self):
        result = _nmea_to_decimal("12301.1000", "W")
        assert result is not None
        assert result < 0

    def test_invalid_input(self):
        assert _nmea_to_decimal("", "N") is None
        assert _nmea_to_decimal("notanumber", "N") is None


# ── Status parsing ────────────────────────────────────────────────────────────

class TestStatusParsing:
    def test_all_clear(self):
        # FFFFFFFF = all bits 1 → negative logic means nothing active
        flags = _parse_status("FFFFFFFF")
        assert not any(flags.values())

    def test_malformed(self):
        flags = _parse_status("ZZZZ")
        assert flags == {}

    def test_empty(self):
        flags = _parse_status("")
        assert flags == {}


# ── Text packet parsing ───────────────────────────────────────────────────────

class TestTextV1Parsing:
    VALID_V1 = (
        b"*HQ,123456789012345,V1,103045,A,3745.9502,N,12301.1000,E,000.0,000,150124,FFFFFFFF#"
    )

    def test_parse_valid_v1(self):
        pkt = parse_packet(self.VALID_V1)
        assert pkt is not None
        assert pkt.imei == "123456789012345"
        assert pkt.packet_type == "text_v1"
        assert pkt.valid_fix is True
        assert pkt.timestamp is not None
        assert pkt.timestamp.tzinfo == timezone.utc
        assert pkt.timestamp.hour == 10
        assert pkt.timestamp.minute == 30
        assert pkt.timestamp.second == 45
        assert pkt.latitude is not None
        assert abs(pkt.latitude - 37.76584) < 0.0001
        assert pkt.longitude is not None
        assert abs(pkt.longitude - 123.01833) < 0.0001
        assert pkt.speed_kmh == 0.0
        assert pkt.course_deg == 0.0

    def test_invalid_fix(self):
        data = self.VALID_V1.replace(b",A,", b",V,")
        pkt = parse_packet(data)
        assert pkt is not None
        assert pkt.valid_fix is False

    def test_returns_none_for_empty(self):
        assert parse_packet(b"") is None

    def test_returns_none_for_unknown(self):
        pkt = parse_packet(b"GARBAGE_PACKET")
        assert pkt is None


class TestTextV5Parsing:
    VALID_V5 = (
        b"*HQ,123456789012345,V5,103045,A,3745.9502,N,12301.1000,E,000.0,000,"
        b"150124,FFFFFFFF,505,01,1A2B,3C4D#"
    )

    def test_parse_valid_v5(self):
        pkt = parse_packet(self.VALID_V5)
        assert pkt is not None
        assert pkt.packet_type == "text_v5"
        assert "cell" in pkt.extra
        assert pkt.extra["cell"]["mcc"] == "505"
        assert pkt.extra["cell"]["mnc"] == "01"


# ── Heartbeat ─────────────────────────────────────────────────────────────────

class TestHeartbeat:
    def test_link_heartbeat(self):
        pkt = parse_packet(b"*HQ,123456789012345,Link#")
        assert pkt is not None
        assert pkt.packet_type == "heartbeat"
        assert pkt.imei == "123456789012345"
        assert pkt.valid_fix is False
        assert pkt.latitude is None


# ── Binary packet parsing ─────────────────────────────────────────────────────

class TestBinaryParsing:
    def _make_binary_packet(self, imei_bcd=b"\x12\x34\x56\x78\x90",
                             time_bcd=b"\x10\x30\x45",
                             lat_bcd=b"\x37\x45\x95\x02",
                             lon_bcd=b"\x12\x30\x11\x00",
                             speed=0, course=0,
                             status=b"\xff\xff\xff\xff"):
        payload = imei_bcd + time_bcd + lat_bcd + lon_bcd + bytes([speed]) + \
                  course.to_bytes(2, "big") + status
        checksum = 0
        for b in payload:
            checksum ^= b
        return b"$" + payload + bytes([checksum])

    def test_parse_binary_packet(self):
        pkt = parse_packet(self._make_binary_packet())
        assert pkt is not None
        assert pkt.packet_type == "binary"
        assert pkt.speed_kmh == 0.0

    def test_binary_too_short(self):
        from h02_parser import H02ParseError
        with pytest.raises(H02ParseError):
            parse_packet(b"$\x00\x01\x02")


# ── ACK building ─────────────────────────────────────────────────────────────

class TestAckBuilding:
    def test_ack_format(self):
        ack = build_ack("123456789012345")
        assert ack.startswith(b"*HQ,123456789012345,R12,")
        assert ack.endswith(b"#")

    def test_ack_time_format(self):
        ack = build_ack("000000000000001").decode()
        # extract time part
        parts = ack.split(",")
        assert len(parts) == 4
        time_part = parts[3].rstrip("#")
        assert len(time_part) == 6
        assert time_part.isdigit()


# ── to_dict serialization ─────────────────────────────────────────────────────

class TestSerialization:
    def test_to_dict_has_timestamp_iso(self):
        pkt = parse_packet(
            b"*HQ,123456789012345,V1,103045,A,3745.9502,N,12301.1000,E,000.0,000,150124,FFFFFFFF#"
        )
        d = pkt.to_dict()
        assert isinstance(d["timestamp"], str)
        assert "T" in d["timestamp"]

    def test_to_dict_none_timestamp(self):
        from h02_parser import ParsedPacket
        from datetime import datetime
        pkt = ParsedPacket(
            imei="test", timestamp=None, latitude=None, longitude=None,
            speed_kmh=None, course_deg=None, valid_fix=False, packet_type="unknown"
        )
        d = pkt.to_dict()
        assert d["timestamp"] is None
