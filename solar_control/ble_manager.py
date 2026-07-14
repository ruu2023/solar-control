"""BLE transport for the Renogy BT-1/BT-2 module (via bleak)."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

logger = logging.getLogger(__name__)

WRITE_SERVICE_UUID = "0000ffd0-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffd1-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"

# Renogy's BT-1/BT-2 modules advertise under these name prefixes.
DEVICE_NAME_PREFIXES = ("BT-TH", "BT-2")


async def scan(timeout: float = 8.0) -> list[BLEDevice]:
    """Discover nearby BLE devices and return the ones that look like a Renogy BT module."""
    devices = await BleakScanner.discover(timeout=timeout)
    return [d for d in devices if d.name and d.name.startswith(DEVICE_NAME_PREFIXES)]


class RoverBleTransport:
    """Request/response transport over the BT-1/BT-2 module's GATT characteristics.

    The module always answers a write with exactly one notification, so each
    call to `request()` writes a frame and waits for the matching reply.
    Concurrent calls are serialized with a lock since the protocol has no
    request ID to disambiguate overlapping replies.
    """

    def __init__(self, mac_address: str, response_timeout: float = 5.0):
        self.mac_address = mac_address
        self.response_timeout = response_timeout
        self._client: Optional[BleakClient] = None
        self._lock = asyncio.Lock()
        self._pending: Optional[asyncio.Future] = None

    async def connect(self) -> None:
        self._client = BleakClient(self.mac_address)
        await self._client.connect()
        await self._client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        logger.info("Connected to %s", self.mac_address)

    async def disconnect(self) -> None:
        if self._client and self._client.is_connected:
            await self._client.stop_notify(NOTIFY_CHAR_UUID)
            await self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    def _on_notify(self, _handle: int, data: bytearray) -> None:
        if self._pending and not self._pending.done():
            self._pending.set_result(bytes(data))

    async def request(self, frame: bytes) -> bytes:
        if not self.is_connected:
            raise RuntimeError("Not connected to the BT module")
        async with self._lock:
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            await self._client.write_gatt_char(WRITE_CHAR_UUID, frame, response=False)
            try:
                return await asyncio.wait_for(self._pending, timeout=self.response_timeout)
            finally:
                self._pending = None
