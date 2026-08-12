"""Encoding for the Shelly Pro 3EM-compatible Modbus input-register block.

The register type (Input Registers, FC04), the addressing offset (documented
Shelly address minus 30000) and the little-endian byte-/word-order below are
confirmed by two independent, real-world Shelly Pro 3EM Modbus TCP clients —
not guessed, and not fully derivable from Shelly's own Modbus documentation,
which specifies addresses/lengths but not function code, addressing
convention, or byte order:

  - github.com/pipelka/dbus-modbus-shelly — register.py: ``Reg_f32l`` coding
    ``('<f', '<2H')``; shelly.py: ``Reg_text(0, 6, '/Serial', little=True)``,
    ``Reg_f32l(1013, '/Ac/Power', ...)``.
  - github.com/dduransseau/shelly-modbus-client — src/client.py:
    ``read_input_registers``, ``struct.pack('<H', ...)`` per register;
    src/consts.py: ``DEVICE_MAC_REGISTER = (0, 6)``, ``EM_REGISTERS`` starting
    at protocol address 1000 (= documented address 31000 minus 30000).

Only active-power values are backed by real AstraMeter data — the
``Powermeter`` abstraction has no voltage/current/frequency/power-factor
telemetry (the existing Shelly UDP/JSON-RPC emulator has the same
limitation: ``shelly.py::_create_em_response`` only ever reports
``*_act_power`` fields). Every other numeric/boolean register in the block
is deliberately left at its zero-init default rather than filled with a
fabricated "plausible" reading (e.g. a constant 230V) — see the startup log
warning in ``server.py`` and the README note.
"""

from __future__ import annotations

import struct
import time

# --- Device-info registers (documented Shelly address minus 30000) ---
DEVICE_MAC_ADDRESS = 0
DEVICE_MAC_REGISTER_COUNT = 6
DEVICE_MODEL_ADDRESS = 6
DEVICE_MODEL_REGISTER_COUNT = 10
DEVICE_NAME_ADDRESS = 16
DEVICE_NAME_REGISTER_COUNT = 32

# --- EM live-measurement registers (Pro 3EM, single EM component) ---
EM_TIMESTAMP_ADDRESS = 1000
EM_TOTAL_ACTIVE_POWER_ADDRESS = 1013
EM_PHASE_A_ACTIVE_POWER_ADDRESS = 1024
EM_PHASE_B_ACTIVE_POWER_ADDRESS = 1044
EM_PHASE_C_ACTIVE_POWER_ADDRESS = 1064

# --- EMDATA registers (Pro 3EM, accumulated energy) ---
# Confirmed required in practice: the BLUETTI Balco Transfer Hub polls this
# exact range (FC04, address 1162, count 64 -> covers 1162-1225) on every
# cycle, not just once at pairing -- see docs/decisions/0002-implementation-
# plan.md "Ergebnis Schritt 5" in the astrameter-modbus-project repo.
# Addresses per github.com/dduransseau/shelly-modbus-client (src/consts.py
# EMDATA_REGISTERS), same -30000 offset convention as the EM block above.
EMDATA_TIMESTAMP_ADDRESS = 1160
EMDATA_TOTAL_FORWARD_ADDRESS = 1162
EMDATA_TOTAL_REVERSE_ADDRESS = 1164
EMDATA_PHASE_A_FORWARD_ADDRESS = 1170
EMDATA_PHASE_A_REVERSE_ADDRESS = 1174
EMDATA_PHASE_B_FORWARD_ADDRESS = 1190
EMDATA_PHASE_B_REVERSE_ADDRESS = 1194
EMDATA_PHASE_C_FORWARD_ADDRESS = 1210
EMDATA_PHASE_C_REVERSE_ADDRESS = 1214
# Last EMDATA register pair Balco reads: address 1224 + 2 registers = 1225
# inclusive. Only total/per-phase forward+reverse Wh above are real
# (integrated from measured watts, see energy.py); reactive energy (VARh),
# "fundamental" energy and the perpetual-count duplicates in this range have
# no computable basis (would need phase-angle/VAR measurement AstraMeter
# doesn't have -- the same class of gap as voltage/current/frequency) and
# are deliberately left at the block's zero-init default, never fabricated.

# Size of the Input Register block backing the whole device: covers device
# info (0-47), the EM range (1000-1073) and the EMDATA range (1160-1225)
# with no internal gaps, so a client reading any contiguous sub-range within
# it never hits "Illegal Data Address" — Modbus has no partial-response
# mechanism, a single invalid address anywhere in a requested range fails
# the whole read (confirmed against the Modbus exception-code-02 semantics,
# independent of any specific client's behavior).
REGISTER_BLOCK_SIZE = 1230


def encode_ascii(text: str, register_count: int) -> list[int]:
    """Encode *text* into *register_count* little-endian-packed registers,
    zero-padded/truncated to fit (matches Shelly's "zero padded" ASCII
    fields)."""
    raw = text.encode("ascii", errors="replace")
    raw = raw.ljust(2 * register_count, b"\x00")[: 2 * register_count]
    return list(struct.unpack(f"<{register_count}H", raw))


def encode_float32(value: float) -> list[int]:
    """Encode *value* as two little-endian registers (low word first)."""
    return list(struct.unpack("<2H", struct.pack("<f", value)))


def encode_uint32(value: int) -> list[int]:
    """Encode *value* as two little-endian registers (low word first)."""
    return list(struct.unpack("<2H", struct.pack("<I", value)))


def build_device_info_registers(
    mac: str, model: str, name: str
) -> dict[int, list[int]]:
    """Static device-identity registers, written once when the server starts."""
    return {
        DEVICE_MAC_ADDRESS: encode_ascii(mac, DEVICE_MAC_REGISTER_COUNT),
        DEVICE_MODEL_ADDRESS: encode_ascii(model, DEVICE_MODEL_REGISTER_COUNT),
        DEVICE_NAME_ADDRESS: encode_ascii(name, DEVICE_NAME_REGISTER_COUNT),
    }


def phase_powers(powers: list[float]) -> tuple[float, float, float]:
    """Mirror ``astrameter.shelly.shelly.Shelly._create_em_response``'s phase
    distribution: a single aggregate reading maps to phase A only, three
    readings map directly, anything else yields no data."""
    if len(powers) == 1:
        return powers[0], 0.0, 0.0
    if len(powers) == 3:
        return powers[0], powers[1], powers[2]
    return 0.0, 0.0, 0.0


def build_em_power_registers(powers: list[float]) -> dict[int, list[int]]:
    """Live active-power registers — the only EM fields backed by real data."""
    a, b, c = phase_powers(powers)
    return {
        EM_TIMESTAMP_ADDRESS: encode_uint32(int(time.time())),
        EM_TOTAL_ACTIVE_POWER_ADDRESS: encode_float32(a + b + c),
        EM_PHASE_A_ACTIVE_POWER_ADDRESS: encode_float32(a),
        EM_PHASE_B_ACTIVE_POWER_ADDRESS: encode_float32(b),
        EM_PHASE_C_ACTIVE_POWER_ADDRESS: encode_float32(c),
    }


def build_emdata_registers(
    total_forward_wh: float,
    total_reverse_wh: float,
    phase_a_forward_wh: float,
    phase_a_reverse_wh: float,
    phase_b_forward_wh: float,
    phase_b_reverse_wh: float,
    phase_c_forward_wh: float,
    phase_c_reverse_wh: float,
) -> dict[int, list[int]]:
    """Real, integrated energy counters (Wh) — the only EMDATA fields backed
    by actual data (see energy.EnergyAccumulator). Reactive energy,
    "fundamental" energy and the perpetual-count duplicates in this range
    are intentionally not covered here; they stay at the block's zero-init
    default."""
    return {
        EMDATA_TIMESTAMP_ADDRESS: encode_uint32(int(time.time())),
        EMDATA_TOTAL_FORWARD_ADDRESS: encode_float32(total_forward_wh),
        EMDATA_TOTAL_REVERSE_ADDRESS: encode_float32(total_reverse_wh),
        EMDATA_PHASE_A_FORWARD_ADDRESS: encode_float32(phase_a_forward_wh),
        EMDATA_PHASE_A_REVERSE_ADDRESS: encode_float32(phase_a_reverse_wh),
        EMDATA_PHASE_B_FORWARD_ADDRESS: encode_float32(phase_b_forward_wh),
        EMDATA_PHASE_B_REVERSE_ADDRESS: encode_float32(phase_b_reverse_wh),
        EMDATA_PHASE_C_FORWARD_ADDRESS: encode_float32(phase_c_forward_wh),
        EMDATA_PHASE_C_REVERSE_ADDRESS: encode_float32(phase_c_reverse_wh),
    }
