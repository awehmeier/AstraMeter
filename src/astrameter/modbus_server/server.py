"""Modbus TCP output server emulating a Shelly Pro 3EM for third-party hubs
(e.g. BLUETTI Balco Transfer Hub) that expect to read live meter data over
Modbus TCP rather than the UDP/JSON-RPC protocol used by ``astrameter.shelly``.
"""

import asyncio
import contextlib

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import ModbusTcpServer

from astrameter.config.logger import debug_traceback, logger
from astrameter.modbus_server.energy import EnergyAccumulator
from astrameter.modbus_server.registers import (
    REGISTER_BLOCK_SIZE,
    build_device_info_registers,
    build_em_power_registers,
    build_emdata_registers,
    phase_powers,
)
from astrameter.powermeter import Powermeter

# Modbus function code for Input Registers (see registers.py docstring for
# why Input Registers rather than Holding Registers).
_FC_INPUT_REGISTERS = 4

_UNMEASURED_FIELDS_WARNING = (
    "Modbus output only reports active power (W) per phase/total — the "
    "only quantity AstraMeter actually measures. Voltage, current, "
    "apparent power, power factor, frequency and all error-flag registers "
    "are NOT real measurements; they are left at 0/False rather than a "
    "fabricated 'plausible' value. See README.md."
)


class ModbusOutputServer:
    """Async Modbus TCP server; lifecycle mirrors ``WebServer``/``Shelly``."""

    def __init__(
        self,
        bind_host: str,
        port: int,
        powermeter: Powermeter,
        device_mac: str = "",
        device_model: str = "",
        device_name: str = "",
        unit_id: int = 1,
        refresh_interval: float = 1.0,
        energy_state_file: str = "modbus_energy_state.json",
        energy_persist_interval: float = 60.0,
    ):
        self._bind_host = bind_host
        self._port = port
        self._powermeter = powermeter
        self._device_mac = device_mac
        self._device_model = device_model
        self._device_name = device_name
        self._unit_id = unit_id
        self._refresh_interval = refresh_interval
        self._energy = EnergyAccumulator(energy_state_file, energy_persist_interval)
        self._server: ModbusTcpServer | None = None
        self._refresh_task: asyncio.Task | None = None
        store = ModbusSlaveContext(
            ir=ModbusSequentialDataBlock(0, [0] * REGISTER_BLOCK_SIZE),
            zero_mode=True,
        )
        self._context = ModbusServerContext(slaves={unit_id: store}, single=False)

    @property
    def port(self) -> int:
        """The bound TCP port (resolves ephemeral port 0 after ``start()``)."""
        if self._server is not None:
            sockets = self._server.transport.sockets  # type: ignore[attr-defined]
            if sockets:
                return sockets[0].getsockname()[1]
        return self._port

    async def start(self) -> bool:
        """Bind the TCP port and start serving. Returns True on success.

        Note: ``ModbusTcpServer.listen()`` (pymodbus 3.6.5) catches bind
        errors (e.g. EADDRINUSE) internally, logs a warning itself, and
        returns ``False`` rather than raising — unlike aiohttp's
        ``TCPSite.start()``. So failure is detected via the return value,
        not a try/except.
        """
        server = ModbusTcpServer(self._context, address=(self._bind_host, self._port))
        if not await server.listen():
            logger.error(
                "Modbus TCP server failed to bind to %s:%s (see warning above "
                "for the underlying error).",
                self._bind_host,
                self._port,
            )
            return False
        self._server = server

        for address, values in build_device_info_registers(
            self._device_mac, self._device_model, self._device_name
        ).items():
            self._set_input_registers(address, values)

        await self._refresh_once()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

        logger.info(
            "Modbus TCP output server listening on %s:%s (unit %s)",
            self._bind_host,
            self.port,
            self._unit_id,
        )
        logger.warning(_UNMEASURED_FIELDS_WARNING)
        return True

    async def stop(self):
        """Shut down the server and release the port."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None
        await self._energy.flush()
        if self._server is not None:
            await self._server.shutdown()
            self._server = None
            logger.info("Modbus TCP output server stopped")

    def is_running(self) -> bool:
        return self._server is not None

    def _set_input_registers(self, address: int, values: list[int]) -> None:
        """Write raw register values at *address* (function code 4/input).

        Used by the live-data refresh loop and by tests to seed known
        values without needing a real client round-trip.
        """
        self._context[self._unit_id].setValues(_FC_INPUT_REGISTERS, address, values)

    async def _refresh_loop(self):
        try:
            while True:
                await asyncio.sleep(self._refresh_interval)
                await self._refresh_once()
        except asyncio.CancelledError:
            pass

    async def _refresh_once(self):
        try:
            powers = await self._powermeter.get_powermeter_watts()
        except Exception as exc:
            logger.warning(
                "Could not read meter values for Modbus output (%s): %s",
                type(self._powermeter).__name__,
                exc,
                exc_info=debug_traceback(),
            )
            return
        for address, values in build_em_power_registers(powers).items():
            self._set_input_registers(address, values)

        a, b, c = phase_powers(powers)
        self._energy.accumulate((a, b, c))
        totals = self._energy.totals
        for address, values in build_emdata_registers(
            totals.total_forward_wh,
            totals.total_reverse_wh,
            totals.phase_a_forward_wh,
            totals.phase_a_reverse_wh,
            totals.phase_b_forward_wh,
            totals.phase_b_reverse_wh,
            totals.phase_c_forward_wh,
            totals.phase_c_reverse_wh,
        ).items():
            self._set_input_registers(address, values)
        await self._energy.maybe_persist()
