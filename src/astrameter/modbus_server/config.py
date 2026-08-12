from dataclasses import dataclass


@dataclass
class ModbusServerConfig:
    """[MODBUS_SERVER] settings — see config_loader.read_modbus_server_config."""

    bind_host: str
    port: int = 502
    unit_id: int = 1
    device_mac: str = ""
    # Real Shelly Pro 3EM model string, confirmed via two independent Modbus
    # clients (see registers.py docstring) — used as the default so the
    # register is populated out of the box, not left blank.
    device_model: str = "SPEM-003CEBEU"
    refresh_interval: float = 1.0
    # Cross-restart energy (Wh) counter state -- see energy.EnergyAccumulator.
    energy_state_file: str = "modbus_energy_state.json"
    energy_persist_interval: float = 60.0
