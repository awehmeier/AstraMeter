import json

import pytest

from astrameter.modbus_server.energy import EnergyAccumulator


def _fake_clock(*ticks: float):
    """A deterministic clock() replacement, injected instead of
    monkeypatching the global time.monotonic -- which would also disrupt
    asyncio's own internal scheduling clock."""
    it = iter(ticks)
    return lambda: next(it)


def test_first_accumulate_call_only_establishes_baseline(tmp_path):
    """No energy is attributed to the first reading -- we don't know how
    long it had already held before this process started measuring it."""
    acc = EnergyAccumulator(str(tmp_path / "state.json"), clock=_fake_clock(100.0))
    acc.accumulate((100.0, 0.0, 0.0))
    assert acc.totals.total_forward_wh == 0.0


def test_accumulate_uses_measured_elapsed_time_not_fixed_interval(tmp_path):
    acc = EnergyAccumulator(
        str(tmp_path / "state.json"),
        clock=_fake_clock(100.0, 3700.0),  # exactly 3600s (1h) later
    )
    acc.accumulate((100.0, 0.0, 0.0))  # baseline
    acc.accumulate((100.0, 0.0, 0.0))  # 1h later at 100W -> 100Wh
    assert acc.totals.total_forward_wh == pytest.approx(100.0)
    assert acc.totals.phase_a_forward_wh == pytest.approx(100.0)


def test_accumulate_splits_import_and_export_by_sign(tmp_path):
    acc = EnergyAccumulator(
        str(tmp_path / "state.json"), clock=_fake_clock(0.0, 3600.0)
    )
    acc.accumulate((100.0, -50.0, 0.0))  # baseline
    acc.accumulate((100.0, -50.0, 0.0))  # 1h later: A imports, B exports
    assert acc.totals.phase_a_forward_wh == pytest.approx(100.0)
    assert acc.totals.phase_a_reverse_wh == 0.0
    assert acc.totals.phase_b_reverse_wh == pytest.approx(50.0)
    assert acc.totals.phase_b_forward_wh == 0.0
    # Total = sum of phases (50W net import) -> forward bucket only
    assert acc.totals.total_forward_wh == pytest.approx(50.0)
    assert acc.totals.total_reverse_wh == 0.0


async def test_no_backfill_across_restart(tmp_path):
    """A long gap *within* one continuous run is legitimately integrated
    (the process never stopped watching). A gap caused by a *restart* must
    NOT be backfilled -- a fresh EnergyAccumulator's first accumulate()
    call only re-establishes the baseline from the persisted total,
    regardless of how much wall-clock time passed since the last flush."""
    state_path = str(tmp_path / "state.json")

    first_run = EnergyAccumulator(state_path, clock=_fake_clock(0.0, 3600.0, 3600.0))
    first_run.accumulate((100.0, 0.0, 0.0))  # baseline
    first_run.accumulate((100.0, 0.0, 0.0))  # +100Wh after a real 1h tick
    assert first_run.totals.total_forward_wh == pytest.approx(100.0)
    await first_run.flush()  # flush() also reads the clock once, for the 3rd tick

    # Simulate restart after a huge downtime: brand-new instance/clock.
    second_run = EnergyAccumulator(
        state_path, clock=_fake_clock(999_999.0, 1_003_599.0)
    )
    second_run.accumulate((100.0, 0.0, 0.0))  # baseline only -- no backfill
    assert second_run.totals.total_forward_wh == pytest.approx(100.0), (
        "the downtime gap before this restart must not be integrated"
    )
    second_run.accumulate((100.0, 0.0, 0.0))  # a real 1h tick within this run
    assert second_run.totals.total_forward_wh == pytest.approx(200.0)


async def test_flush_and_reload_persists_across_instances(tmp_path):
    state_path = str(tmp_path / "state.json")
    acc = EnergyAccumulator(state_path, clock=_fake_clock(0.0, 3600.0, 3600.0))
    acc.accumulate((200.0, 0.0, 0.0))
    acc.accumulate((200.0, 0.0, 0.0))
    assert acc.totals.total_forward_wh == pytest.approx(200.0)

    await acc.flush()  # flush() also reads the clock once, for the 3rd tick
    assert (tmp_path / "state.json").exists()

    reloaded = EnergyAccumulator(state_path)
    assert reloaded.totals.total_forward_wh == pytest.approx(200.0)
    assert reloaded.totals.phase_a_forward_wh == pytest.approx(200.0)


def test_missing_state_file_starts_at_zero(tmp_path):
    acc = EnergyAccumulator(str(tmp_path / "does_not_exist.json"))
    assert acc.totals.total_forward_wh == 0.0


def test_corrupt_state_file_starts_at_zero_without_crashing(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    acc = EnergyAccumulator(str(path))
    assert acc.totals.total_forward_wh == 0.0


async def test_maybe_persist_is_debounced(tmp_path):
    state_path = tmp_path / "state.json"
    # maybe_persist() reads the clock once; the first call also flushes,
    # consuming a second tick inside flush(). The second call (10s later,
    # well under persist_interval=60) reads the clock once and returns.
    acc = EnergyAccumulator(
        str(state_path), persist_interval=60.0, clock=_fake_clock(0.0, 0.0, 10.0)
    )

    await acc.maybe_persist()  # first call always flushes
    first_mtime = state_path.stat().st_mtime_ns

    await acc.maybe_persist()  # 10s later, well under the 60s interval
    assert state_path.stat().st_mtime_ns == first_mtime, "should not have re-written"


async def test_maybe_persist_flushes_again_after_interval_elapses(tmp_path):
    state_path = tmp_path / "state.json"
    acc = EnergyAccumulator(
        str(state_path), persist_interval=60.0, clock=_fake_clock(0.0, 0.0, 90.0, 90.0)
    )

    await acc.maybe_persist()  # flushes at t=0
    assert acc._last_persisted_monotonic == 0.0

    await acc.maybe_persist()  # 90s later, past the 60s interval -> flushes again
    assert acc._last_persisted_monotonic == 90.0, "should have re-flushed"


async def test_flush_writes_valid_json_with_all_fields(tmp_path):
    state_path = tmp_path / "state.json"
    acc = EnergyAccumulator(str(state_path))
    await acc.flush()
    data = json.loads(state_path.read_text())
    for field in (
        "total_forward_wh",
        "total_reverse_wh",
        "phase_a_forward_wh",
        "phase_a_reverse_wh",
        "phase_b_forward_wh",
        "phase_b_reverse_wh",
        "phase_c_forward_wh",
        "phase_c_reverse_wh",
    ):
        assert field in data
