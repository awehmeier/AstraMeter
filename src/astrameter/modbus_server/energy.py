"""Cross-restart energy (Wh) accumulation for the EMDATA Modbus registers.

Integrates real, measured active power (W) over actual elapsed wall-clock
time (``time.monotonic()`` deltas, not a fixed nominal tick interval, so
scheduling jitter/system load never skews the running total) into
imported ("forward") and exported ("reverse") Wh counters, per phase and
total. Persisted to a small JSON state file so a restart doesn't reset the
counter to 0 -- matching how a real energy meter keeps its counter in
flash across reboots. Deliberately does NOT backfill the time the process
was not running: that would estimate energy for a period with no real
measurement, exactly the kind of fabricated value this project avoids
elsewhere (see registers.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, fields

from astrameter.config.logger import logger


@dataclass
class EnergyTotals:
    total_forward_wh: float = 0.0
    total_reverse_wh: float = 0.0
    phase_a_forward_wh: float = 0.0
    phase_a_reverse_wh: float = 0.0
    phase_b_forward_wh: float = 0.0
    phase_b_reverse_wh: float = 0.0
    phase_c_forward_wh: float = 0.0
    phase_c_reverse_wh: float = 0.0


class EnergyAccumulator:
    def __init__(
        self,
        state_path: str,
        persist_interval: float = 60.0,
        clock=time.monotonic,
    ):
        """*clock* defaults to ``time.monotonic``; tests inject a fake one
        instead of monkeypatching the global function, which would also
        disrupt asyncio's own internal scheduling clock."""
        self._state_path = state_path
        self._persist_interval = max(0.0, persist_interval)
        self._clock = clock
        self.totals = EnergyTotals()
        self._last_tick_monotonic: float | None = None
        self._last_persisted_monotonic: float | None = None
        self._load()

    def _load(self) -> None:
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
            self.totals = EnergyTotals(
                **{f.name: float(data.get(f.name, 0.0)) for f in fields(EnergyTotals)}
            )
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            logger.warning(
                "Could not read Modbus energy state file %s (%s); starting from 0 Wh.",
                self._state_path,
                exc,
            )

    def accumulate(self, powers: tuple[float, float, float]) -> None:
        """Integrate one measurement using the real elapsed time since the
        previous call. The first call after (re)start only establishes the
        baseline timestamp -- no energy is attributed to it, since we don't
        know how long the previous reading actually held."""
        now = self._clock()
        if self._last_tick_monotonic is None:
            self._last_tick_monotonic = now
            return
        dt_hours = (now - self._last_tick_monotonic) / 3600.0
        self._last_tick_monotonic = now
        if dt_hours <= 0:
            return

        a, b, c = powers
        self._integrate("total", a + b + c, dt_hours)
        self._integrate("phase_a", a, dt_hours)
        self._integrate("phase_b", b, dt_hours)
        self._integrate("phase_c", c, dt_hours)

    def _integrate(self, prefix: str, power_w: float, dt_hours: float) -> None:
        if power_w >= 0:
            attr = f"{prefix}_forward_wh"
            setattr(self.totals, attr, getattr(self.totals, attr) + power_w * dt_hours)
        else:
            attr = f"{prefix}_reverse_wh"
            setattr(
                self.totals, attr, getattr(self.totals, attr) + (-power_w) * dt_hours
            )

    async def maybe_persist(self) -> None:
        """Flush at most once per ``persist_interval`` (debounced)."""
        now = self._clock()
        if (
            self._last_persisted_monotonic is not None
            and now - self._last_persisted_monotonic < self._persist_interval
        ):
            return
        await self.flush()

    async def flush(self) -> None:
        """Force a write now (e.g. on clean shutdown). Runs the blocking
        file I/O in a thread so it never stalls the event loop."""
        self._last_persisted_monotonic = self._clock()
        await asyncio.to_thread(self._write_state_file)

    def _write_state_file(self) -> None:
        directory = os.path.dirname(self._state_path) or "."
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=directory, prefix=".energy-", suffix=".tmp"
            )
        except OSError as exc:
            logger.warning(
                "Could not persist Modbus energy state to %s: %s",
                self._state_path,
                exc,
            )
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(self.totals), f)
            os.replace(tmp_path, self._state_path)
        except OSError as exc:
            os.unlink(tmp_path)
            logger.warning(
                "Could not persist Modbus energy state to %s: %s",
                self._state_path,
                exc,
            )
