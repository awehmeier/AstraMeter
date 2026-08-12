import asyncio
import struct

import pytest
from pymodbus.client import AsyncModbusTcpClient

from astrameter.modbus_server import ModbusOutputServer
from astrameter.powermeter import Powermeter


class FakePowermeter(Powermeter):
    def __init__(self, watts: list[float]):
        self.watts = watts

    async def get_powermeter_watts(self) -> list[float]:
        return self.watts


class FailingPowermeter(Powermeter):
    async def get_powermeter_watts(self) -> list[float]:
        raise RuntimeError("meter offline")


def _decode_float32(registers: list[int]) -> float:
    return struct.unpack("<f", struct.pack("<2H", *registers))[0]


def _energy_state_path(tmp_path) -> str:
    """Every test gets its own state file -- the default filename lives in
    the current working directory, which would otherwise leak energy
    accumulation between unrelated tests."""
    return str(tmp_path / "energy.json")


async def test_start_and_stop_lifecycle(tmp_path):
    server = ModbusOutputServer(
        "127.0.0.1",
        0,
        FakePowermeter([0.0, 0.0, 0.0]),
        energy_state_file=_energy_state_path(tmp_path),
    )
    assert await server.start() is True
    try:
        assert server.is_running()
        assert server.port != 0
    finally:
        await server.stop()
    assert not server.is_running()


async def test_second_server_on_same_port_fails_to_bind(tmp_path):
    first = ModbusOutputServer(
        "127.0.0.1",
        0,
        FakePowermeter([0.0, 0.0, 0.0]),
        energy_state_file=_energy_state_path(tmp_path),
    )
    assert await first.start() is True
    try:
        second = ModbusOutputServer(
            "127.0.0.1",
            first.port,
            FakePowermeter([0.0, 0.0, 0.0]),
            energy_state_file=str(tmp_path / "energy2.json"),
        )
        assert await second.start() is False
        assert not second.is_running()
    finally:
        await first.stop()


async def test_e2e_client_reads_live_power_from_powermeter(tmp_path):
    server = ModbusOutputServer(
        "127.0.0.1",
        0,
        FakePowermeter([100.0, 200.0, 300.0]),
        device_mac="AABBCCDDEEFF",
        device_model="SPEM-003CEBEU",
        device_name="shellypro3em-test",
        unit_id=1,
        refresh_interval=0.05,
        energy_state_file=_energy_state_path(tmp_path),
    )
    await server.start()
    try:
        client = AsyncModbusTcpClient("127.0.0.1", port=server.port)
        await client.connect()
        try:
            # Total active power at protocol address 1013 (documented 31013).
            result = await client.read_input_registers(1013, count=2, slave=1)
            assert not result.isError()
            assert _decode_float32(result.registers) == pytest.approx(600.0)

            # Phase A/B/C active power at 1024/1044/1064 (31024/31044/31064).
            phase_a = await client.read_input_registers(1024, count=2, slave=1)
            phase_b = await client.read_input_registers(1044, count=2, slave=1)
            phase_c = await client.read_input_registers(1064, count=2, slave=1)
            assert _decode_float32(phase_a.registers) == pytest.approx(100.0)
            assert _decode_float32(phase_b.registers) == pytest.approx(200.0)
            assert _decode_float32(phase_c.registers) == pytest.approx(300.0)

            # Device model at protocol address 6 (documented 30006).
            model = await client.read_input_registers(6, count=10, slave=1)
            assert not model.isError()
            raw = struct.pack("<10H", *model.registers).rstrip(b"\x00")
            assert raw.decode("ascii") == "SPEM-003CEBEU"
        finally:
            client.close()
    finally:
        await server.stop()


async def test_e2e_live_value_updates_within_one_refresh_interval(tmp_path):
    powermeter = FakePowermeter([0.0, 0.0, 0.0])
    server = ModbusOutputServer(
        "127.0.0.1",
        0,
        powermeter,
        unit_id=1,
        refresh_interval=0.05,
        energy_state_file=_energy_state_path(tmp_path),
    )
    await server.start()
    try:
        client = AsyncModbusTcpClient("127.0.0.1", port=server.port)
        await client.connect()
        try:
            initial = await client.read_input_registers(1013, count=2, slave=1)
            assert _decode_float32(initial.registers) == pytest.approx(0.0)

            powermeter.watts = [500.0, 0.0, 0.0]
            await client.read_input_registers(0, count=1, slave=1)  # let time pass
            await asyncio.sleep(0.15)

            updated = await client.read_input_registers(1013, count=2, slave=1)
            assert _decode_float32(updated.registers) == pytest.approx(500.0)
        finally:
            client.close()
    finally:
        await server.stop()


async def test_unmeasured_fields_default_to_zero_not_fabricated(tmp_path):
    server = ModbusOutputServer(
        "127.0.0.1",
        0,
        FakePowermeter([1.0, 2.0, 3.0]),
        refresh_interval=0.05,
        energy_state_file=_energy_state_path(tmp_path),
    )
    await server.start()
    try:
        client = AsyncModbusTcpClient("127.0.0.1", port=server.port)
        await client.connect()
        try:
            # Phase A voltage (documented 31020 -> protocol address 1020):
            # never written by the refresh loop, must stay at the zero-init
            # default rather than a fabricated nominal value.
            voltage = await client.read_input_registers(1020, count=2, slave=1)
            assert _decode_float32(voltage.registers) == 0.0

            # Reactive energy (documented 31178 -> protocol address 1178):
            # no phase-angle/VAR measurement available, same principle.
            reactive = await client.read_input_registers(1178, count=2, slave=1)
            assert _decode_float32(reactive.registers) == 0.0
        finally:
            client.close()
    finally:
        await server.stop()


async def test_refresh_survives_powermeter_error(tmp_path):
    server = ModbusOutputServer(
        "127.0.0.1",
        0,
        FailingPowermeter(),
        refresh_interval=0.05,
        energy_state_file=_energy_state_path(tmp_path),
    )
    assert await server.start() is True
    try:
        assert server.is_running()
    finally:
        await server.stop()


async def test_e2e_balco_polling_pattern_no_illegal_address(tmp_path):
    """Regression test for the exact scenario found in the first real Balco
    hardware test run (see docs/decisions/0002-implementation-plan.md
    "Ergebnis Schritt 5" in the astrameter-modbus-project repo): Balco reads
    FC04 address 1011 count 64 (EM block) and FC04 address 1162 count 64
    (EMDATA block) every poll cycle. Both must succeed -- neither may raise
    Illegal Data Address."""
    server = ModbusOutputServer(
        "127.0.0.1",
        0,
        FakePowermeter([100.0, 200.0, 300.0]),
        refresh_interval=0.05,
        energy_state_file=_energy_state_path(tmp_path),
    )
    await server.start()
    try:
        client = AsyncModbusTcpClient("127.0.0.1", port=server.port)
        await client.connect()
        try:
            em_block = await client.read_input_registers(1011, count=64, slave=1)
            assert not em_block.isError(), em_block

            emdata_block = await client.read_input_registers(1162, count=64, slave=1)
            assert not emdata_block.isError(), emdata_block
        finally:
            client.close()
    finally:
        await server.stop()


async def test_e2e_emdata_total_forward_energy_is_real_and_nonzero(tmp_path):
    server = ModbusOutputServer(
        "127.0.0.1",
        0,
        FakePowermeter([100.0, 0.0, 0.0]),
        refresh_interval=0.02,
        energy_state_file=_energy_state_path(tmp_path),
    )
    await server.start()
    try:
        await asyncio.sleep(0.2)  # let several refresh ticks integrate energy
        client = AsyncModbusTcpClient("127.0.0.1", port=server.port)
        await client.connect()
        try:
            total_forward = await client.read_input_registers(1162, count=2, slave=1)
            assert _decode_float32(total_forward.registers) > 0.0

            # Never-exported meter: reverse bucket stays exactly 0.
            total_reverse = await client.read_input_registers(1164, count=2, slave=1)
            assert _decode_float32(total_reverse.registers) == 0.0
        finally:
            client.close()
    finally:
        await server.stop()


async def test_energy_state_persists_across_server_restarts(tmp_path):
    state_path = _energy_state_path(tmp_path)
    powermeter = FakePowermeter([300.0, 0.0, 0.0])

    server = ModbusOutputServer(
        "127.0.0.1", 0, powermeter, refresh_interval=0.02, energy_state_file=state_path
    )
    await server.start()
    await asyncio.sleep(0.2)
    client = AsyncModbusTcpClient("127.0.0.1", port=server.port)
    await client.connect()
    first_run = await client.read_input_registers(1162, count=2, slave=1)
    client.close()
    first_value = _decode_float32(first_run.registers)
    assert first_value > 0.0
    await server.stop()  # flushes state to disk on clean stop

    # A brand-new server instance, same state file: must resume, not reset.
    server2 = ModbusOutputServer(
        "127.0.0.1", 0, powermeter, refresh_interval=0.02, energy_state_file=state_path
    )
    await server2.start()
    try:
        client2 = AsyncModbusTcpClient("127.0.0.1", port=server2.port)
        await client2.connect()
        try:
            immediately_after_restart = await client2.read_input_registers(
                1162, count=2, slave=1
            )
            # Small tolerance: a little more real integration may have
            # happened between the read above and this one, but it must
            # never have dropped back toward 0.
            assert (
                _decode_float32(immediately_after_restart.registers)
                >= first_value - 0.01
            )
        finally:
            client2.close()
    finally:
        await server2.stop()
