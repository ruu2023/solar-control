"""Modbus-over-BLE framing for the Renogy BT-1/BT-2 bluetooth module.

The Rover speaks Modbus RTU internally. The BT-1/BT-2 module bridges this to
BLE: requests are written to characteristic 0xFFD1 (service 0xFFD0) and the
Rover's reply arrives as a single notification on characteristic 0xFFF1.
Frame layout is identical to Modbus RTU (device id, function, payload,
CRC16) - only the transport (GATT instead of a serial wire) differs.

Register map below is reverse-engineered from Renogy's own MODBUS
documentation and cross-checked against the community `renogy-bt` project.
"""
from __future__ import annotations

FUNCTION_READ = 3
FUNCTION_WRITE = 6
EXCEPTION_BIT = 0x80


class ModbusError(Exception):
    """Raised when a response frame is too short, fails CRC, or is a Modbus exception reply."""

# device_id 255 works for a single controller wired directly to a BT-1/BT-2
# module. Only change this if you're daisy-chaining multiple controllers
# through a hub (see Renogy's MODBUS documentation).
DEFAULT_DEVICE_ID = 255

# Known register blocks as (register_address, word_count). Registers are
# 16-bit words; word_count * 2 = payload byte count.
REG_DEVICE_INFO = (12, 8)       # model name string
REG_DEVICE_ADDRESS = (26, 1)    # controller's own modbus address
REG_CHARGING_INFO = (256, 34)   # battery/PV/load telemetry block
REG_BATTERY_TYPE = (57348, 1)

# Load (DC output) on/off control. function=6 (write single register),
# value 0 = off, 1 = on.
REG_LOAD_CONTROL = 266


def crc16_modbus(data: bytes) -> bytes:
    """Standard Modbus RTU CRC16 (poly 0xA001, init 0xFFFF).

    Returns the 2-byte CRC in wire order (low byte first, high byte
    second) - verified against the community renogy-bt implementation's
    table-based CRC for several real request frames.
    """
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def build_read_request(register: int, words: int, device_id: int = DEFAULT_DEVICE_ID) -> bytes:
    return _build_request(device_id, FUNCTION_READ, register, words)


def build_write_request(register: int, value: int, device_id: int = DEFAULT_DEVICE_ID) -> bytes:
    return _build_request(device_id, FUNCTION_WRITE, register, value)


def _build_request(device_id: int, function: int, register: int, value: int) -> bytes:
    body = bytes([
        device_id,
        function,
        (register >> 8) & 0xFF,
        register & 0xFF,
        (value >> 8) & 0xFF,
        value & 0xFF,
    ])
    return body + crc16_modbus(body)


def validate_response(data: bytes, expected_function: int) -> bytes:
    """Check a response frame's length, CRC, and function code before it's parsed.

    A Modbus exception reply is only 5 bytes (id, function|0x80, exception_code,
    crc_lo, crc_hi), so indexing into it as if it were a normal reply reads
    garbage or raises IndexError. Raising here instead surfaces the actual
    bytes the device sent, which is what you need to debug a bad reply.
    """
    if len(data) < 5:
        raise ModbusError(f"response too short ({len(data)} bytes): {data.hex()}")
    if data[-2:] != crc16_modbus(data[:-2]):
        raise ModbusError(f"CRC mismatch in response: {data.hex()}")
    if data[1] & EXCEPTION_BIT:
        raise ModbusError(f"device returned Modbus exception code {data[2]}: {data.hex()}")
    if data[1] != expected_function:
        raise ModbusError(f"unexpected function code {data[1]} (wanted {expected_function}): {data.hex()}")
    return data


def bytes_to_int(data: bytes, offset: int, length: int, signed: bool = False, scale: float = 1) -> float:
    """Big-endian integer extraction, matching the Rover's register layout."""
    chunk = data[offset:offset + length]
    if len(chunk) < length:
        return 0
    value = int.from_bytes(chunk, byteorder="big", signed=signed)
    return round(value * scale, 2)
