"""
TCP Server — listens for Sinotrack/H02 device connections.

Sinotrack devices maintain a persistent TCP connection and send packets
at configured intervals. Each connection maps to one IMEI/device.

Flow:
  device connects → sends packet(s) → server ACKs → device sends next → …
"""

import asyncio
import logging
import time
from collections import defaultdict
from typing import Callable, Optional

from h02_parser import parse_packet, build_ack, ParsedPacket, H02ParseError

logger = logging.getLogger(__name__)

# Callback type: (ParsedPacket) → None
PacketCallback = Callable[[ParsedPacket], None]


class DeviceConnection:
    """Manages a single persistent TCP connection from a GPS device."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_packet: PacketCallback,
        ack_enabled: bool = True,
        stats: Optional[dict] = None,
    ):
        self.reader      = reader
        self.writer      = writer
        self.on_packet   = on_packet
        self.ack_enabled = ack_enabled
        self.stats       = stats if stats is not None else {}
        self.imei: Optional[str] = None
        self.remote_addr = writer.get_extra_info("peername", ("unknown", 0))

    async def handle(self) -> None:
        peer = f"{self.remote_addr[0]}:{self.remote_addr[1]}"
        logger.info("New connection from %s", peer)
        self.stats.setdefault("connections", 0)
        self.stats["connections"] += 1

        try:
            while True:
                # H02 text packets end with '#'; binary packets have a fixed length.
                # Read up to 512 bytes; most packets are < 100 bytes.
                data = await asyncio.wait_for(
                    self._read_packet(),
                    timeout=300,   # 5-minute idle timeout
                )
                if not data:
                    logger.info("Connection closed by %s", peer)
                    break

                logger.debug("← [%s] raw: %s", peer, data[:120])
                self.stats.setdefault("packets_received", 0)
                self.stats["packets_received"] += 1
                self.stats["last_seen"] = time.time()

                try:
                    pkt = parse_packet(data)
                except H02ParseError as e:
                    logger.warning("Parse error from %s: %s", peer, e)
                    continue

                if pkt is None:
                    continue

                if self.imei is None and pkt.imei:
                    self.imei = pkt.imei
                    logger.info("Device IMEI identified: %s (%s)", self.imei, peer)

                # ACK text packets so device doesn't retransmit
                if self.ack_enabled and pkt.packet_type not in ("binary", "heartbeat"):
                    ack = build_ack(pkt.imei)
                    self.writer.write(ack)
                    await self.writer.drain()
                    logger.debug("→ [%s] ACK: %s", peer, ack.decode())

                self.stats.setdefault("packets_parsed", 0)
                self.stats["packets_parsed"] += 1

                # Fire callback (sync — publisher is fast)
                try:
                    self.on_packet(pkt)
                except Exception as e:
                    logger.error("Packet callback error: %s", e)

        except asyncio.TimeoutError:
            logger.info("Idle timeout for %s (IMEI: %s)", peer, self.imei or "unknown")
        except asyncio.IncompleteReadError:
            logger.info("Connection reset by %s", peer)
        except Exception as e:
            logger.exception("Unexpected error for %s: %s", peer, e)
        finally:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            logger.info("Connection closed: %s (IMEI: %s)", peer, self.imei or "unknown")

    async def _read_packet(self) -> bytes:
        """
        Read one H02 packet. Handles:
          - Text packets: start with '*' or '*HQ', end with '#'
          - Binary packets: start with 0x24 ($), fixed 25-byte length
          - Raw IMEI heartbeat: 15 ASCII digits followed by newline or next packet
        """
        first = await self.reader.read(1)
        if not first:
            return b""

        # ── Text packet ──────────────────────────────────────────────────
        if first == b"*":
            rest = await self.reader.readuntil(b"#")
            return first + rest

        # ── Binary packet ────────────────────────────────────────────────
        if first == b"$":
            rest = await self.reader.readexactly(24)   # 1 + 24 = 25 bytes total
            return first + rest

        # ── Could be a plain IMEI keepalive or junk ─────────────────────
        # Read until newline or '#', whichever comes first
        rest = await self.reader.readuntil(b"\n", )
        return first + rest.rstrip(b"\n\r")


class TCPServer:
    """Async TCP server that spawns a DeviceConnection per client."""

    def __init__(self, host: str, port: int, on_packet: PacketCallback, ack_enabled: bool = True):
        self.host        = host
        self.port        = port
        self.on_packet   = on_packet
        self.ack_enabled = ack_enabled
        self.stats: dict = defaultdict(dict)
        self._server: Optional[asyncio.Server] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client,
            self.host,
            self.port,
        )
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        logger.info("H02 TCP server listening on %s", addrs)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername", ("?", 0))
        conn_stats: dict = {}
        self.stats[peer] = conn_stats
        conn = DeviceConnection(
            reader, writer,
            on_packet=self.on_packet,
            ack_enabled=self.ack_enabled,
            stats=conn_stats,
        )
        await conn.handle()
        self.stats.pop(peer, None)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            logger.info("TCP server stopped")

    def get_stats(self) -> dict:
        return dict(self.stats)
