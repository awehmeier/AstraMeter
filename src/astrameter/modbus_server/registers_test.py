import struct

import pytest

from astrameter.modbus_server.registers import (
    DEVICE_MAC_ADDRESS,
    DEVICE_MODEL_ADDRESS,
    DEVICE_NAME_ADDRESS,
    EM_PHASE_A_ACTIVE_POWER_ADDRESS,
    EM_PHASE_B_ACTIVE_POWER_ADDRESS,
    EM_PHASE_C_ACTIVE_POWER_ADDRESS,
    EM_TOTAL_ACTIVE_POWER_ADDRESS,
    EMDATA_PHASE_A_FORWARD_ADDRESS,
    EMDATA_PHASE_A_REVERSE_ADDRESS,
    EMDATA_PHASE_B_FORWARD_ADDRESS,
    EMDATA_PHASE_C_FORWARD_ADDRESS,
    EMDATA_TOTAL_FORWARD_ADDRESS,
    EMDATA_TOTAL_REVERSE_ADDRESS,
    build_device_info_registers,
    build_em_power_registers,
    build_emdata_registers,
    encode_ascii,
    encode_float32,
)


def _decode_float32(registers: list[int]) -> float:
    return struct.unpack("<f", struct.pack("<2H", *registers))[0]


def test_encode_float32_zero():
    assert _decode_float32(encode_float32(0.0)) == 0.0


def test_encode_float32_positive_import():
    assert _decode_float32(encode_float32(1234.5)) == pytest.approx(1234.5)


def test_encode_float32_negative_export():
    assert _decode_float32(encode_float32(-987.25)) == pytest.approx(-987.25)


def test_encode_float32_word_order_matches_reference_clients():
    # Cross-check against the exact byte layout used by both reference
    # clients (see registers.py module docstring): low word (bytes 0-1 of
    # the IEEE754 little-endian representation) at the lower address, high
    # word at the higher address.
    raw = struct.pack("<f", 10.0)
    expected = [
        int.from_bytes(raw[0:2], "little"),
        int.from_bytes(raw[2:4], "little"),
    ]
    assert encode_float32(10.0) == expected


def test_encode_ascii_pads_with_zeros():
    registers = encode_ascii("AB", 2)
    # 'A' (0x41) is the low byte, 'B' (0x42) the high byte of register 0
    # (little-endian); register 1 is zero padding.
    assert registers == [0x42 << 8 | 0x41, 0]


def test_encode_ascii_truncates_to_register_count():
    registers = encode_ascii("ABCDEF", 2)
    assert len(registers) == 2


def test_build_device_info_registers_addresses_and_lengths():
    regs = build_device_info_registers(
        "AABBCCDDEEFF", "SPEM-003CEBEU", "shellypro3em-x"
    )
    assert set(regs) == {DEVICE_MAC_ADDRESS, DEVICE_MODEL_ADDRESS, DEVICE_NAME_ADDRESS}
    assert len(regs[DEVICE_MAC_ADDRESS]) == 6
    assert len(regs[DEVICE_MODEL_ADDRESS]) == 10
    assert len(regs[DEVICE_NAME_ADDRESS]) == 32


def test_build_em_power_registers_zero_power():
    regs = build_em_power_registers([0.0, 0.0, 0.0])
    assert _decode_float32(regs[EM_TOTAL_ACTIVE_POWER_ADDRESS]) == 0.0
    assert _decode_float32(regs[EM_PHASE_A_ACTIVE_POWER_ADDRESS]) == 0.0


def test_build_em_power_registers_three_phase_import():
    regs = build_em_power_registers([100.0, 200.0, 300.0])
    assert _decode_float32(regs[EM_PHASE_A_ACTIVE_POWER_ADDRESS]) == pytest.approx(
        100.0
    )
    assert _decode_float32(regs[EM_PHASE_B_ACTIVE_POWER_ADDRESS]) == pytest.approx(
        200.0
    )
    assert _decode_float32(regs[EM_PHASE_C_ACTIVE_POWER_ADDRESS]) == pytest.approx(
        300.0
    )
    assert _decode_float32(regs[EM_TOTAL_ACTIVE_POWER_ADDRESS]) == pytest.approx(600.0)


def test_build_em_power_registers_export_is_negative():
    regs = build_em_power_registers([-50.0, -25.0, -25.0])
    assert _decode_float32(regs[EM_TOTAL_ACTIVE_POWER_ADDRESS]) == pytest.approx(-100.0)


def test_build_em_power_registers_single_aggregate_reading_maps_to_phase_a():
    regs = build_em_power_registers([300.0])
    assert _decode_float32(regs[EM_PHASE_A_ACTIVE_POWER_ADDRESS]) == pytest.approx(
        300.0
    )
    assert _decode_float32(regs[EM_PHASE_B_ACTIVE_POWER_ADDRESS]) == 0.0
    assert _decode_float32(regs[EM_TOTAL_ACTIVE_POWER_ADDRESS]) == pytest.approx(300.0)


def test_build_em_power_registers_unusable_reading_yields_zeros():
    regs = build_em_power_registers([1.0, 2.0])
    assert _decode_float32(regs[EM_TOTAL_ACTIVE_POWER_ADDRESS]) == 0.0


def test_build_emdata_registers_covers_expected_addresses():
    regs = build_emdata_registers(
        total_forward_wh=100.0,
        total_reverse_wh=5.0,
        phase_a_forward_wh=40.0,
        phase_a_reverse_wh=0.0,
        phase_b_forward_wh=30.0,
        phase_b_reverse_wh=1.0,
        phase_c_forward_wh=30.0,
        phase_c_reverse_wh=4.0,
    )
    assert _decode_float32(regs[EMDATA_TOTAL_FORWARD_ADDRESS]) == pytest.approx(100.0)
    assert _decode_float32(regs[EMDATA_TOTAL_REVERSE_ADDRESS]) == pytest.approx(5.0)
    assert _decode_float32(regs[EMDATA_PHASE_A_FORWARD_ADDRESS]) == pytest.approx(40.0)
    assert _decode_float32(regs[EMDATA_PHASE_A_REVERSE_ADDRESS]) == 0.0
    assert _decode_float32(regs[EMDATA_PHASE_B_FORWARD_ADDRESS]) == pytest.approx(30.0)
    assert _decode_float32(regs[EMDATA_PHASE_C_FORWARD_ADDRESS]) == pytest.approx(30.0)


def test_build_emdata_registers_within_register_block_size():
    from astrameter.modbus_server.registers import REGISTER_BLOCK_SIZE

    regs = build_emdata_registers(0, 0, 0, 0, 0, 0, 0, 0)
    for address, values in regs.items():
        assert address + len(values) <= REGISTER_BLOCK_SIZE
