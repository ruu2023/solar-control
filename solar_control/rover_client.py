"""High-level client for reading status from / controlling a Renogy Rover."""
from __future__ import annotations

import asyncio

import logging

from . import protocol
from .ble_manager import RoverBleTransport

logger = logging.getLogger(__name__)

CHARGING_STATE = {
    0: "deactivated",
    1: "activated",
    2: "mppt",
    3: "equalizing",
    4: "boost",
    5: "floating",
    6: "current_limiting",
}


class RoverClient:
    def __init__(
        self,
        mac_address: str,
        device_id: int = protocol.DEFAULT_DEVICE_ID,
        temperature_unit: str = "C",
    ):
        self.device_id = device_id
        self.temperature_unit = temperature_unit
        self._transport = RoverBleTransport(mac_address)

    async def connect(self) -> None:
        await self._transport.connect()

    async def disconnect(self) -> None:
        await self._transport.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._transport.is_connected

    async def get_status(self) -> dict:
        register, words = protocol.REG_CHARGING_INFO
        frame = protocol.build_read_request(register, words, self.device_id)
        response = await self._transport.request(frame)
        response = protocol.validate_response(response, protocol.FUNCTION_READ)
        return self._parse_charging_info(response)

    async def set_load(self, on: bool) -> dict:
        """Switch the DC load output. The BT-1 write is fire-and-forget and its
        ack notification is unreliable (often truncated), so a missing/short ack
        is not treated as a failure -- callers should confirm via get_status().
        Genuine transport errors (e.g. disconnected) still propagate."""
        if not self._transport.is_connected:
            raise RuntimeError("Not connected to the BT module")
        frame = protocol.build_write_request(protocol.REG_LOAD_CONTROL, 1 if on else 0, self.device_id)
        acked: bool | None = None
        try:
            response = await self._transport.request(frame)
            response = protocol.validate_response(response, protocol.FUNCTION_WRITE)
            # Write-ack layout: [id][func][reg_hi][reg_lo][value_hi][value_lo][crc_lo][crc_hi]
            if len(response) > 5:
                acked = response[5] == 1
        except (asyncio.TimeoutError, TimeoutError, IndexError, ValueError):
            pass
        return {"load_status": "on" if (acked if acked is not None else on) else "off"}

    def _parse_charging_info(self, data: bytes) -> dict:
        def temp(offset: int) -> float:
            raw = data[offset]
            celsius = -(raw - 128) if (raw >> 7) else raw
            if self.temperature_unit.upper() == "C":
                return celsius
            return round(celsius * 9 / 5 + 32, 1)

        b2i = protocol.bytes_to_int
        return {
            "battery_percent": b2i(data, 3, 2),
            "battery_voltage": b2i(data, 5, 2, scale=0.1),
            "battery_current": b2i(data, 7, 2, scale=0.01),
            "controller_temperature": temp(9),
            "battery_temperature": temp(10),
            "load_voltage": b2i(data, 11, 2, scale=0.1),
            "load_current": b2i(data, 13, 2, scale=0.01),
            "load_power": b2i(data, 15, 2),
            "pv_voltage": b2i(data, 17, 2, scale=0.1),
            "pv_current": b2i(data, 19, 2, scale=0.01),
            "pv_power": b2i(data, 21, 2),
            "max_charge_power_today": b2i(data, 33, 2),
            "max_discharge_power_today": b2i(data, 35, 2),
            "charge_amp_hours_today": b2i(data, 37, 2),
            "discharge_amp_hours_today": b2i(data, 39, 2),
            "power_generation_today": b2i(data, 41, 2),
            "power_consumption_today": b2i(data, 43, 2),
            "power_generation_total_wh": b2i(data, 59, 4),
            "load_status": "on" if (data[67] >> 7) else "off",
            "charging_status": CHARGING_STATE.get(data[68], "unknown"),
        }
