import argparse
import configparser
from ipaddress import IPv4Network

from astrameter import main as main_module
from astrameter.config.config_loader import ClientFilter
from astrameter.main import (
    _resolve_device_config,
    _select_default_powermeter,
    _select_shellypro3em_device_id,
    _should_start_modbus_server,
    read_ct_powermeter,
)
from astrameter.powermeter import Powermeter


class _StubPowermeter(Powermeter):
    """Minimal powermeter stub for testing ``read_ct_powermeter``."""

    def __init__(
        self,
        values: list[float],
        wait_raises: BaseException | None = None,
        wait_calls: list[float] | None = None,
    ):
        self._values = values
        self._wait_raises = wait_raises
        self._wait_calls = wait_calls if wait_calls is not None else []

    async def get_powermeter_watts(self) -> list[float]:
        return list(self._values)

    async def get_powermeter_watts_raw(self) -> list[float]:
        return list(self._values)

    async def wait_for_next_message(self, timeout=5):
        self._wait_calls.append(timeout)
        if self._wait_raises is not None:
            raise self._wait_raises


_LOCAL = ClientFilter([IPv4Network("127.0.0.1/32")])


async def test_read_ct_powermeter_returns_none_when_no_match():
    pm = _StubPowermeter([10.0])
    powermeters = [(pm, _LOCAL, True)]
    assert await read_ct_powermeter(("10.0.0.1", 0), powermeters) is None


async def test_read_ct_powermeter_pads_to_three_phases():
    pm = _StubPowermeter([42.0])
    powermeters = [(pm, _LOCAL, False)]
    assert await read_ct_powermeter(("127.0.0.1", 0), powermeters) == [42.0, 0, 0]


async def test_read_ct_powermeter_skips_wait_when_disabled():
    pm = _StubPowermeter([1.0, 2.0, 3.0])
    powermeters = [(pm, _LOCAL, False)]
    result = await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert result == [1.0, 2.0, 3.0]
    assert pm._wait_calls == []


async def test_read_ct_powermeter_calls_wait_with_2s_when_enabled():
    pm = _StubPowermeter([1.0, 2.0, 3.0])
    powermeters = [(pm, _LOCAL, True)]
    await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert pm._wait_calls == [2]


async def test_stub_powermeter_raw_matches_watts():
    pm = _StubPowermeter([3.0, 4.0, 5.0])
    assert (
        await pm.get_powermeter_watts_raw()
        == await pm.get_powermeter_watts()
        == [
            3.0,
            4.0,
            5.0,
        ]
    )


async def test_read_ct_powermeter_swallows_timeout_and_serves_cached():
    """Issue #327: a slow push meter must not break CT002 responses."""
    pm = _StubPowermeter(
        [11.0, 22.0, 33.0],
        wait_raises=TimeoutError("simulated slow meter"),
    )
    powermeters = [(pm, _LOCAL, True)]
    result = await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert result == [11.0, 22.0, 33.0]


def _resolve(device_type: str) -> tuple[list[str], list[str]]:
    cfg = configparser.ConfigParser()
    cfg.add_section("GENERAL")
    cfg.set("GENERAL", "DEVICE_TYPE", device_type)
    args = argparse.Namespace(
        device_types=None, skip_powermeter_test=None, device_ids=None
    )
    device_types, device_ids, _ = _resolve_device_config(cfg, args)
    return device_types, device_ids


def test_resolve_device_config_shellypro3em_new_gets_shelly_id():
    """Issue #389: explicit shellypro3em_new must use a shellypro3em-* id."""
    device_types, device_ids = _resolve("shellypro3em_new")
    assert device_types == ["shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_old_gets_shelly_id():
    device_types, device_ids = _resolve("shellypro3em_old")
    assert device_types == ["shellypro3em_old"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_expands_to_old_and_new():
    device_types, device_ids = _resolve("shellypro3em")
    assert device_types == ["shellypro3em_old", "shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1", "shellypro3em-ec4609c439c1"]


def test_should_start_modbus_server_true_for_either_shellypro3em_variant():
    assert _should_start_modbus_server(["shellypro3em_old"])
    assert _should_start_modbus_server(["shellypro3em_new"])
    assert _should_start_modbus_server(["shellypro3em_old", "shellypro3em_new"])


def test_should_start_modbus_server_false_without_shellypro3em():
    assert not _should_start_modbus_server([])
    assert not _should_start_modbus_server(["ct002"])
    assert not _should_start_modbus_server(["shellyemg3", "shellyproem50"])


def test_should_start_modbus_server_evaluated_once_not_per_variant():
    """Even though a bare "shellypro3em" always expands to both _old and
    _new (same device_id, see test_resolve_device_config_shellypro3em_expands_
    to_old_and_new above), the gate is a single boolean over the whole
    device_types list -- callers must invoke this once, not per entry, or
    the Modbus server would start twice."""
    device_types, _ = _resolve("shellypro3em")
    assert _should_start_modbus_server(device_types) is True
    # A single call over the full list, not one call per device_type.
    assert isinstance(_should_start_modbus_server(device_types), bool)


def test_select_shellypro3em_device_id_finds_first_match():
    device_types, device_ids = _resolve("shellypro3em")
    assert _select_shellypro3em_device_id(device_types, device_ids) == (
        "shellypro3em-ec4609c439c1"
    )


def test_select_shellypro3em_device_id_none_without_match():
    assert _select_shellypro3em_device_id(["ct002"], ["device-1"]) is None


def test_select_default_powermeter_prefers_wildcard_filter():
    wildcard = ClientFilter([IPv4Network("0.0.0.0/0")])
    specific = _LOCAL
    pm_specific = _StubPowermeter([1.0])
    pm_wildcard = _StubPowermeter([2.0])
    powermeters = [(pm_specific, specific, True), (pm_wildcard, wildcard, True)]
    assert _select_default_powermeter(powermeters) is pm_wildcard


def test_select_default_powermeter_falls_back_to_first():
    pm = _StubPowermeter([1.0])
    assert _select_default_powermeter([(pm, _LOCAL, True)]) is pm


def test_select_default_powermeter_none_when_empty():
    assert _select_default_powermeter([]) is None


def _patch_modbus_server_wiring(monkeypatch) -> list[dict]:
    """Stub out async_main()'s powermeter loading, run_device() (which would
    otherwise bind the real, fixed 1010/2220 Shelly UDP ports) and
    ModbusOutputServer, returning the list of kwargs each fake construction
    was called with."""
    stub_pm = _StubPowermeter([1.0, 2.0, 3.0])
    wildcard = ClientFilter([IPv4Network("0.0.0.0/0")])
    monkeypatch.setattr(
        main_module,
        "read_all_powermeter_configs",
        lambda cfg: [(stub_pm, wildcard, False)],
    )

    async def _noop_run_device(*args, **kwargs):
        return None

    monkeypatch.setattr(main_module, "run_device", _noop_run_device)

    constructions: list[dict] = []

    class _FakeModbusOutputServer:
        def __init__(self, **kwargs):
            constructions.append(kwargs)

        async def start(self):
            return True

        async def stop(self):
            pass

    monkeypatch.setattr(main_module, "ModbusOutputServer", _FakeModbusOutputServer)
    # Whenever the (faked) Modbus server starts, async_main() also
    # constructs a real MdnsAnnouncer, which would otherwise try actual
    # multicast networking during these tests. Default it to a safe no-op
    # here; tests that care about mDNS specifically call
    # _patch_mdns_wiring() afterward to override this with an inspectable
    # fake (monkeypatch.setattr application order means the later call wins).
    _patch_mdns_wiring(monkeypatch)
    return constructions


def _patch_mdns_wiring(monkeypatch, start_result: bool = True) -> list[dict]:
    """Stub out MdnsAnnouncer, returning the list of kwargs each fake
    construction was called with, plus recording start()/stop() calls on
    each instance via the returned dicts' "calls" key."""
    calls: list[dict] = []

    class _FakeMdnsAnnouncer:
        def __init__(self, **kwargs):
            self._calls = {"started": False, "stopped": False, **kwargs}
            calls.append(self._calls)

        async def start(self):
            self._calls["started"] = True
            return start_result

        async def stop(self):
            self._calls["stopped"] = True

    monkeypatch.setattr(main_module, "MdnsAnnouncer", _FakeMdnsAnnouncer)
    return calls


async def _run_async_main_with_modbus_section(
    modbus_section: dict, device_types: list[str], device_ids: list[str]
) -> None:
    cfg = configparser.ConfigParser()
    cfg["GENERAL"] = {"ENABLE_WEB_SERVER": "false"}
    cfg["MODBUS_SERVER"] = modbus_section
    args = argparse.Namespace(config="config.ini")
    await main_module.async_main(
        cfg,
        args,
        device_types=device_types,
        device_ids=device_ids,
        skip_test=True,
        managed_marstek=None,
    )


async def test_async_main_starts_modbus_server_exactly_once_for_old_and_new(
    monkeypatch,
):
    """Even though a configured "shellypro3em" always expands to both
    shellypro3em_old and shellypro3em_new (same device_id -- see
    test_resolve_device_config_shellypro3em_expands_to_old_and_new), the
    Modbus output server must start exactly once, not once per UDP-listener
    variant -- async_main() evaluates _should_start_modbus_server()/
    constructs ModbusOutputServer a single time regardless of how many
    device_types entries match "shellypro3em"."""
    constructions = _patch_modbus_server_wiring(monkeypatch)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true", "BIND_HOST": "127.0.0.1"},
        device_types=["shellypro3em_old", "shellypro3em_new"],
        device_ids=["shellypro3em-x", "shellypro3em-x"],
    )

    assert len(constructions) == 1
    assert constructions[0]["device_name"] == "shellypro3em-x"


async def test_async_main_skips_modbus_server_without_shellypro3em(monkeypatch):
    constructions = _patch_modbus_server_wiring(monkeypatch)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true", "BIND_HOST": "127.0.0.1"},
        device_types=["ct002"],
        device_ids=["device-1"],
    )

    assert constructions == []


async def test_async_main_skips_modbus_server_when_bind_host_missing(monkeypatch):
    """ADR 0001 point 12 (astrameter-modbus-project repo): Modbus TCP has no
    built-in authentication, so an unset BIND_HOST must NOT silently fall
    back to binding every interface -- the server simply does not start."""
    constructions = _patch_modbus_server_wiring(monkeypatch)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true"},  # no BIND_HOST at all
        device_types=["shellypro3em_old", "shellypro3em_new"],
        device_ids=["shellypro3em-x", "shellypro3em-x"],
    )

    assert constructions == []


async def test_async_main_skips_modbus_server_when_bind_host_empty(monkeypatch):
    constructions = _patch_modbus_server_wiring(monkeypatch)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true", "BIND_HOST": ""},
        device_types=["shellypro3em_old", "shellypro3em_new"],
        device_ids=["shellypro3em-x", "shellypro3em-x"],
    )

    assert constructions == []


async def test_async_main_passes_configured_bind_host_through_unchanged(monkeypatch):
    """A BIND_HOST the user explicitly set -- including "0.0.0.0", if that
    is a deliberate choice -- is passed to ModbusOutputServer unmodified;
    only an unset/empty value is rejected (see the missing-BIND_HOST tests
    above)."""
    constructions = _patch_modbus_server_wiring(monkeypatch)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true", "BIND_HOST": "192.168.1.50"},
        device_types=["shellypro3em_old", "shellypro3em_new"],
        device_ids=["shellypro3em-x", "shellypro3em-x"],
    )

    assert len(constructions) == 1
    assert constructions[0]["bind_host"] == "192.168.1.50"


async def test_async_main_announces_mdns_when_modbus_server_starts(monkeypatch):
    _patch_modbus_server_wiring(monkeypatch)
    mdns_calls = _patch_mdns_wiring(monkeypatch)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true", "BIND_HOST": "192.168.1.50"},
        device_types=["shellypro3em_old", "shellypro3em_new"],
        device_ids=["shellypro3em-x", "shellypro3em-x"],
    )

    assert len(mdns_calls) == 1
    assert mdns_calls[0]["bind_host"] == "192.168.1.50"
    assert mdns_calls[0]["started"] is True
    assert mdns_calls[0]["stopped"] is True  # async_main() shut down cleanly


async def test_async_main_skips_mdns_when_modbus_server_fails_to_start(monkeypatch):
    """Advertising a device with nothing listening behind it would just be
    misleading -- mDNS is gated on the Modbus server actually coming up."""

    class _FailingModbusOutputServer:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            return False

        async def stop(self):
            pass

    monkeypatch.setattr(main_module, "ModbusOutputServer", _FailingModbusOutputServer)
    mdns_calls = _patch_mdns_wiring(monkeypatch)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true", "BIND_HOST": "192.168.1.50"},
        device_types=["shellypro3em_old", "shellypro3em_new"],
        device_ids=["shellypro3em-x", "shellypro3em-x"],
    )

    assert mdns_calls == []


async def test_async_main_keeps_modbus_server_running_when_mdns_start_fails(
    monkeypatch,
):
    """mDNS.start() returning False must not crash async_main() -- it just
    means the device isn't discoverable; the Modbus server itself, already
    listening, is left running."""
    constructions = _patch_modbus_server_wiring(monkeypatch)
    _patch_mdns_wiring(monkeypatch, start_result=False)

    await _run_async_main_with_modbus_section(
        {"ENABLED": "true", "BIND_HOST": "192.168.1.50"},
        device_types=["shellypro3em_old", "shellypro3em_new"],
        device_ids=["shellypro3em-x", "shellypro3em-x"],
    )

    assert len(constructions) == 1  # Modbus server was still constructed and started
