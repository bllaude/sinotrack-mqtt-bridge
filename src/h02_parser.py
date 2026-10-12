"""
H02 Protocol Parser for Sinotrack GPS Trackers
Handles both text (*HQ,...#) and binary ($...) packet formats.

Packet examples:
  Text:   *HQ,123456789012345,V1,103045,A,3745.9502,N,12301.1000,E,000.0,000,150124,FFFFFFFF#
  Binary: $<IMEI 5B><time 3B><lat 4B><lon 4B><speed 1B><course 2B><status 1B><checksum 1B>
"""

import struct
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ParsedPacket:
    imei: str
    timestamp: Optional[datetime]
    latitude: Optional[float]
    longitude: Optional[float]
    speed_kmh: Optional[float]
    course_deg: Optional[float]
    valid_fix: bool
    packet_type: str  # "text_v1", "text_v5", "binary", "heartbeat", "unknown"
    raw_status: Optional[str] = None
    alarm: Optional[str] = None
    voltage: Optional[float] = None
    signal: Optional[int] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.timestamp:
            d["timestamp"] = self.timestamp.isoformat()
        return d


class H02ParseError(Exception):
    pass


def is_valid_imei(imei: str) -> bool:
    """Validate a 15-digit IMEI with the Luhn checksum."""
    if not isinstance(imei, str) or len(imei) != 15 or not imei.isdigit():
        return False
    total = 0
    for i, ch in enumerate(imei):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# ── Status / alarm bitmask (vehicle_status field, 4 bytes hex) ──────────────

STATUS_FLAGS = {
    "sos":        (3, 0),   # byte index, bit
    "power_cut":  (3, 1),
    "vibration":  (3, 2),
    "door_open":  (3, 3),
    "acc_on":     (2, 4),
    "charging":   (2, 5),
    "engine_cut": (1, 0),
    "low_battery":(1, 1),
    "overspeed":  (0, 0),
    "geofence":   (0, 1),
}


def _parse_status(hex_str: str) -> dict:
    """Parse 4-byte hex status word into a dict of flag->bool."""
    try:
        val = int(hex_str, 16)
        raw = [(val >> (i * 8)) & 0xFF for i in range(4)]
    except (ValueError, TypeError):
        return {}

    result = {}
    for name, (byte_idx, bit) in STATUS_FLAGS.items():
        if byte_idx < len(raw):
            # H02 uses negative logic: bit=0 means active
            result[name] = not bool(raw[byte_idx] & (1 << bit))
    return result


def _nmea_to_decimal(raw: str, direction: str) -> Optional[float]:
    """Convert NMEA DDDMM.MMMM + N/S/E/W to decimal degrees."""
    try:
        raw = raw.strip()
        dot_pos = raw.index(".")
        # degrees occupy all digits before last 2 digits before the dot
        deg_digits = dot_pos - 2
        if deg_digits < 1:
            return None
        degrees = float(raw[:deg_digits])
        minutes = float(raw[deg_digits:])
        decimal = degrees + minutes / 60.0
        if direction in ("S", "W"):
            decimal = -decimal
        return round(decimal, 7)
    except Exception:
        return None


def _parse_text_v1(parts: list[str], imei: str) -> ParsedPacket:
    """
    *HQ,<imei>,V1,<HHMMSS>,<A/V>,<lat>,<N/S>,<lon>,<E/W>,<speed>,<course>,<DDMMYY>,<status>#
    """
    try:
        time_str = parts[3]    # HHMMSS
        fix_flag = parts[4]    # A=valid, V=invalid
        lat_raw  = parts[5]
        lat_dir  = parts[6]
        lon_raw  = parts[7]
        lon_dir  = parts[8]
        speed    = float(parts[9])   # knots
        course   = float(parts[10])
        date_str = parts[11]         # DDMMYY
        status   = parts[12].rstrip("#").strip() if len(parts) > 12 else "00000000"

        dt = datetime.strptime(date_str + time_str, "%d%m%y%H%M%S").replace(tzinfo=timezone.utc)
        lat = _nmea_to_decimal(lat_raw, lat_dir)
        lon = _nmea_to_decimal(lon_raw, lon_dir)
        flags = _parse_status(status)
        alarm = next((k for k, v in flags.items() if v and k in
                      ("sos", "power_cut", "vibration", "low_battery", "overspeed")), None)

        return ParsedPacket(
            imei=imei,
            timestamp=dt,
            latitude=lat,
            longitude=lon,
            speed_kmh=round(speed * 1.852, 2),   # knots → km/h
            course_deg=course,
            valid_fix=(fix_flag == "A"),
            packet_type="text_v1",
            raw_status=status,
            alarm=alarm,
            extra={"flags": flags},
        )
    except Exception as e:
        raise H02ParseError(f"V1 parse error: {e}") from e


def _parse_text_v5(parts: list[str], imei: str) -> ParsedPacket:
    """
    *HQ,<imei>,V5,<HHMMSS>,<A/V>,<lat>,<N/S>,<lon>,<E/W>,<speed>,<course>,<DDMMYY>,<status>,
    <mcc>,<mnc>,<lac>,<cellid>#
    Mostly the same layout as V1 with extra cell tower fields.
    """
    pkt = _parse_text_v1(parts, imei)
    pkt.packet_type = "text_v5"
    try:
        if len(parts) >= 17:
            pkt.extra["cell"] = {
                "mcc":    parts[13],
                "mnc":    parts[14],
                "lac":    parts[15],
                "cellid": parts[16].rstrip("#").strip(),
            }
    except Exception:
        pass
    return pkt


def _parse_binary(data: bytes) -> ParsedPacket:
    """
    Binary H02 packet starting with 0x24 ($):
    Byte 0     : 0x24 ($)
    Bytes 1-5  : IMEI (BCD, 5 bytes = 10 digits; some devices 8 bytes = 15 digits)
    Bytes 6-8  : Time (BCD: HH MM SS)
    Bytes 9-12 : Latitude (BCD: DD MM SS.SS)
    Bytes 13-16: Longitude (BCD: DDD MM SS.SS)
    Byte 17    : Speed (binary, km/h)
    Bytes 18-19: Course (big-endian, degrees)
    Bytes 20-23: Status flags (4 bytes)
    Byte 24    : Checksum (XOR of bytes 1..23)
    """
    if len(data) < 25:
        raise H02ParseError(f"Binary packet too short: {len(data)} bytes")

    # Verify checksum
    checksum = 0
    for b in data[1:24]:
        checksum ^= b
    if checksum != data[24]:
        logger.warning("Binary checksum mismatch: expected 0x%02X got 0x%02X", data[24], checksum)

    def bcd_decode(b: bytes) -> str:
        return "".join(f"{byte:02X}" for byte in b)

    # IMEI — first 5 bytes BCD = 10 digits (some firmwares use 8 bytes for 15 digits).
    # Keep leading zeros: stripping them would corrupt IMEIs that start with 0.
    imei = bcd_decode(data[1:6]).rstrip()

    # Time
    hh = int(bcd_decode(data[6:7]))
    mm = int(bcd_decode(data[7:8]))
    ss = int(bcd_decode(data[8:9]))

    # Latitude: bytes 9-12, BCD DDMMSSSS (last 4 digits = fractional seconds)
    lat_raw = bcd_decode(data[9:13])
    lat_deg = int(lat_raw[0:2])
    lat_min = int(lat_raw[2:4])
    lat_sec_frac = int(lat_raw[4:8]) / 10000.0  # fractional seconds expressed per 10000
    latitude = lat_deg + lat_min / 60.0 + lat_sec_frac / 3600.0

    # Longitude: bytes 13-16, BCD DDDMMSSSS
    lon_raw = bcd_decode(data[13:17])
    lon_deg = int(lon_raw[0:3])
    lon_min = int(lon_raw[3:5])
    lon_sec_frac = int(lon_raw[5:9]) / 10000.0
    longitude = lon_deg + lon_min / 60.0 + lon_sec_frac / 3600.0

    speed_kmh = float(data[17])
    course_deg = struct.unpack(">H", data[18:20])[0]

    status_bytes = data[20:24]
    status_hex = status_bytes.hex().upper()
    flags = _parse_status(status_hex)

    # Direction bits in status determine N/S E/W
    # Bit 2 of byte 3 = latitude S if set; Bit 1 of byte 3 = longitude W if set
    status_word = int(status_hex, 16)
    if status_word & (1 << 26):  # bit 26 → south
        latitude = -latitude
    if status_word & (1 << 25):  # bit 25 → west
        longitude = -longitude

    alarm = next((k for k, v in flags.items() if v and k in
                  ("sos", "power_cut", "vibration", "low_battery", "overspeed")), None)

    now = datetime.now(timezone.utc)
    try:
        dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    except ValueError:
        dt = now

    return ParsedPacket(
        imei=imei,
        timestamp=dt,
        latitude=round(latitude, 7),
        longitude=round(longitude, 7),
        speed_kmh=speed_kmh,
        course_deg=float(course_deg),
        valid_fix=True,
        packet_type="binary",
        raw_status=status_hex,
        alarm=alarm,
        extra={"flags": flags},
    )


def parse_packet(data: bytes) -> Optional[ParsedPacket]:
    """
    Entry point. Auto-detects packet format and returns a ParsedPacket or None for heartbeats.
    """
    if not data:
        return None

    text = data.decode("ascii", errors="replace").strip()

    # ── Heartbeat ──────────────────────────────────────────────────────────
    # Some Sinotrack devices send "*HQ,<imei>,Link#"; others send a raw 15-digit IMEI
    if text.startswith("*HQ") and ",Link#" in text:
        imei = text.split(",")[1] if "," in text else "unknown"
        return ParsedPacket(
            imei=imei, timestamp=datetime.now(timezone.utc),
            latitude=None, longitude=None,
            speed_kmh=None, course_deg=None,
            valid_fix=False, packet_type="heartbeat",
        )

    if text.isdigit() and len(text) == 15:
        return ParsedPacket(
            imei=text, timestamp=datetime.now(timezone.utc),
            latitude=None, longitude=None,
            speed_kmh=None, course_deg=None,
            valid_fix=False, packet_type="heartbeat",
        )

    # ── Text H02 ───────────────────────────────────────────────────────────
    if text.startswith("*HQ,"):
        parts = text[1:].split(",")  # remove leading *, parts[0] == "HQ"
        # parts[0] = "HQ", parts[1] = IMEI, parts[2] = cmd
        if len(parts) < 4:
            logger.warning("Malformed text packet: %s", text[:80])
            return None
        imei = parts[1]
        cmd  = parts[2]
        if cmd in ("V1",):
            return _parse_text_v1(parts, imei)
        elif cmd in ("V5", "V6"):
            return _parse_text_v5(parts, imei)
        elif cmd == "V8":
            # V8 = alarm packet (same structure as V1)
            pkt = _parse_text_v1(parts, imei)
            pkt.packet_type = "text_v8_alarm"
            return pkt
        else:
            logger.debug("Unhandled H02 command: %s", cmd)
            return None

    # ── Binary H02 ─────────────────────────────────────────────────────────
    if data[0:1] == b"$":
        return _parse_binary(data)

    logger.debug("Unknown packet format, first byte: 0x%02X", data[0])
    return None


def build_ack(imei: str) -> bytes:
    """
    Server acknowledgement for text V1/V5 packets:
    *HQ,<imei>,R12,<HHMMSS>#
    """
    now = datetime.now(timezone.utc)
    return f"*HQ,{imei},R12,{now.strftime('%H%M%S')}#".encode()