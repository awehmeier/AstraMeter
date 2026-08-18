"""Unit tests for MdnsAnnouncer. Real mDNS is multicast UDP traffic, which
CI runners often can't do reliably -- so these mock AsyncZeroconf rather
than performing a live loopback announcement (see docs/decisions/
0005-native-mdns-plan.md section 4 in the astrameter-modbus-project repo)."""

from typing import ClassVar

import astrameter.mdns as mdns_module
from astrameter.mdns import MdnsAnnouncer


class _FakeAsyncZeroconf:
    instances: ClassVar[list["_FakeAsyncZeroconf"]] = []

    def __init__(self, interfaces=None):
        self.interfaces = interfaces
        self.registered = []
        self.unregistered = []
        self.closed = False
        self.fail_register = False
        _FakeAsyncZeroconf.instances.append(self)

    async def async_register_service(self, info, cooperating_responders=False):
        if self.fail_register:
            raise RuntimeError("simulated bind failure")
        self.registered.append((info, cooperating_responders))

    async def async_unregister_service(self, info):
        self.unregistered.append(info)

    async def async_close(self):
        self.closed = True


def _install_fake(monkeypatch):
    _FakeAsyncZeroconf.instances = []
    monkeypatch.setattr(mdns_module, "AsyncZeroconf", _FakeAsyncZeroconf)


def test_service_info_matches_the_previously_manual_avahi_command():
    """Same name/type/port/TXT as the avahi-publish-service unit it
    replaces (docs/decisions/0003-mdns-deployment-note.md), so existing
    hubs see no behavior change."""
    announcer = MdnsAnnouncer(bind_host="192.168.1.50")
    info = announcer._info
    assert info.type == "_shelly._tcp.local."
    assert info.get_name() == "shellypro3em-astrameter"
    assert info.port == 80
    assert info.properties == {b"gen": b"2", b"app": b"Pro3EM", b"ver": b"1.4.0"}
    assert info.parsed_scoped_addresses() == ["192.168.1.50"]


async def test_start_registers_with_cooperating_responders_true(monkeypatch):
    """cooperating_responders=True lets this coexist with a system
    avahi-daemon that may already be running on the host."""
    _install_fake(monkeypatch)
    announcer = MdnsAnnouncer(bind_host="192.168.1.50")

    assert await announcer.start() is True

    assert announcer.is_running()
    zc = _FakeAsyncZeroconf.instances[0]
    assert zc.interfaces == ["192.168.1.50"]
    (_, cooperating_responders) = zc.registered[0]
    assert cooperating_responders is True


async def test_start_returns_false_and_closes_on_registration_failure(monkeypatch):
    _install_fake(monkeypatch)
    original_init = _FakeAsyncZeroconf.__init__

    def failing_init(self, interfaces=None):
        original_init(self, interfaces)
        self.fail_register = True

    monkeypatch.setattr(_FakeAsyncZeroconf, "__init__", failing_init)
    announcer = MdnsAnnouncer(bind_host="192.168.1.50")

    assert await announcer.start() is False

    assert not announcer.is_running()
    assert _FakeAsyncZeroconf.instances[0].closed is True


async def test_stop_unregisters_and_closes():
    announcer = MdnsAnnouncer(bind_host="192.168.1.50")
    fake = _FakeAsyncZeroconf()
    announcer._aiozc = fake

    await announcer.stop()

    assert fake.unregistered == [announcer._info]
    assert fake.closed is True
    assert not announcer.is_running()


async def test_stop_is_a_noop_when_never_started():
    announcer = MdnsAnnouncer(bind_host="192.168.1.50")
    await announcer.stop()  # must not raise
    assert not announcer.is_running()
