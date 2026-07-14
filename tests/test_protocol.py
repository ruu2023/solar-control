import pytest

from solar_control import protocol


def test_crc16_matches_known_modbus_vector():
    # Canonical Modbus RTU CRC example (Read Holding Registers request).
    frame = bytes([0x01, 0x03, 0x00, 0x00, 0x00, 0x0A])
    assert protocol.crc16_modbus(frame) == bytes([0xC5, 0xCD])


def test_build_read_request_appends_crc():
    frame = protocol.build_read_request(256, 34, device_id=255)
    assert frame[:6] == bytes([255, 3, 0x01, 0x00, 0x00, 34])
    assert len(frame) == 8


def test_build_write_request_load_on():
    frame = protocol.build_write_request(protocol.REG_LOAD_CONTROL, 1, device_id=255)
    assert frame[:6] == bytes([255, 6, 0x01, 0x0A, 0x00, 0x01])


def test_bytes_to_int_scaling():
    data = bytes([0, 0, 0, 0x00, 0xC8])  # value 200 at offset 3, length 2
    assert protocol.bytes_to_int(data, 3, 2, scale=0.1) == 20.0


def test_validate_response_accepts_well_formed_write_ack():
    body = bytes([255, 6, 0x01, 0x0A, 0x00, 0x01])
    frame = body + protocol.crc16_modbus(body)
    assert protocol.validate_response(frame, protocol.FUNCTION_WRITE) == frame


def test_validate_response_rejects_short_frame():
    with pytest.raises(protocol.ModbusError, match="too short"):
        protocol.validate_response(bytes([255, 6, 0, 0]), protocol.FUNCTION_WRITE)


def test_validate_response_rejects_bad_crc():
    body = bytes([255, 6, 0x01, 0x0A, 0x00, 0x01])
    frame = body + bytes([0x00, 0x00])  # wrong CRC
    with pytest.raises(protocol.ModbusError, match="CRC mismatch"):
        protocol.validate_response(frame, protocol.FUNCTION_WRITE)


def test_validate_response_rejects_modbus_exception():
    # 5-byte Modbus exception reply: id, function|0x80, exception_code, crc_lo, crc_hi
    body = bytes([255, 6 | protocol.EXCEPTION_BIT, 0x02])
    frame = body + protocol.crc16_modbus(body)
    with pytest.raises(protocol.ModbusError, match="exception code 2"):
        protocol.validate_response(frame, protocol.FUNCTION_WRITE)
