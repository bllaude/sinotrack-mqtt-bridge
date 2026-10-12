"""
Integration tests for tcp_server.py — real asyncio TCP client/server round-trip.
Run: pytest tests/test_tcp_server.py -v
"""

import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tcp_server import TCPServer

VALID_V1 = b"*HQ,123456789012345,V1,103045,A,3745.9502,N,12301.1000,E,000.0,000,150124,FFFFFFFF#"
LINK_HB  = b"*HQ,123456789012345,Link#"
RAW_IMEI_HB = b"123456789012345\n"


async def _run_server(on_packet, max_connections=100):
    server = TCPServer("127.0.0.1", 0, on_packet=on_packet,
                       ack_enabled=True, max_connections=max_connections)
    await server.start()
    sock = next(iter(server._server.sockets))
    port = sock.getsockname()[1]
    return server, port


class TestTcpRoundTrip:
    def test_text_packet_and_ack(self):
        received = []

        async def scenario():
            server, port = await _run_server(received.append)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(VALID_V1)
            await writer.drain()
            ack = await asyncio.wait_for(reader.read(64), timeout=2)
            writer.close()
            await server.stop()
            return ack

        ack = asyncio.run(scenario())
        assert len(received) == 1
        assert received[0].imei == "123456789012345"
        assert received[0].packet_type == "text_v1"
        assert ack.startswith(b"*HQ,123456789012345,R12,")
        assert ack.endswith(b"#")

    def test_link_heartbeat(self):
        received = []

        async def scenario():
            server, port = await _run_server(received.append)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(LINK_HB)
            await writer.drain()
            await asyncio.wait_for(reader.read(64), timeout=2)
            writer.close()
            await server.stop()

        asyncio.run(scenario())
        assert len(received) == 1
        assert received[0].packet_type == "heartbeat"

    def test_raw_imei_heartbeat(self):
        """A bare 15-digit IMEI keepalive should be parsed as a heartbeat and ACKed."""
        received = []

        async def scenario():
            server, port = await _run_server(received.append)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(RAW_IMEI_HB)
            await writer.drain()
            ack = await asyncio.wait_for(reader.read(64), timeout=2)
            writer.close()
            await server.stop()
            return ack

        ack = asyncio.run(scenario())
        assert len(received) == 1
        assert received[0].packet_type == "heartbeat"
        assert received[0].imei == "123456789012345"
        assert ack.startswith(b"*HQ,123456789012345,R12,")

    def test_multiple_packets_on_one_connection(self):
        received = []

        async def scenario():
            server, port = await _run_server(received.append)
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(VALID_V1 + LINK_HB)
            await writer.drain()
            await asyncio.sleep(0.3)   # let server process both
            writer.close()
            await server.stop()

        asyncio.run(scenario())
        types = [p.packet_type for p in received]
        assert types == ["text_v1", "heartbeat"]

    def test_max_connections_enforced(self):
        received = []

        async def scenario():
            server, port = await _run_server(received.append, max_connections=1)
            r1, w1 = await asyncio.open_connection("127.0.0.1", port)
            w1.write(VALID_V1)
            await w1.drain()
            await asyncio.wait_for(r1.read(64), timeout=2)  # first conn accepted
            r2, w2 = await asyncio.open_connection("127.0.0.1", port)
            w2.write(VALID_V1)
            await w2.drain()
            # second connection should be rejected → EOF or reset, but no ACK
            try:
                data = await asyncio.wait_for(r2.read(64), timeout=2)
            except (ConnectionResetError, asyncio.IncompleteReadError):
                data = b""
            w1.close(); w2.close()
            await server.stop()
            return data

        data = asyncio.run(scenario())
        assert data == b""                      # rejected, no ACK
        assert len(received) == 1               # only first connection's packet