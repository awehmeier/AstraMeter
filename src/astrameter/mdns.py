"""mDNS (_shelly._tcp) announcement so hubs that discover devices before
connecting (e.g. the BLUETTI Balco Transfer Hub) can find AstraMeter's
Modbus TCP output server. Replaces the external ``avahi-publish-service``
systemd unit described in the astrameter-modbus-project repo's
docs/decisions/0003-mdns-deployment-note.md -- same service name, type,
port and TXT records, just announced in-process instead.
"""

from zeroconf import ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from astrameter.config.logger import logger

_SERVICE_TYPE = "_shelly._tcp.local."
_SERVICE_NAME = "shellypro3em-astrameter"
_ANNOUNCED_PORT = 80
_PROPERTIES = {"gen": "2", "app": "Pro3EM", "ver": "1.4.0"}


class MdnsAnnouncer:
    """Async mDNS service announcer; lifecycle mirrors ``ModbusOutputServer``."""

    def __init__(self, bind_host: str):
        self._bind_host = bind_host
        self._aiozc: AsyncZeroconf | None = None
        self._info = ServiceInfo(
            _SERVICE_TYPE,
            f"{_SERVICE_NAME}.{_SERVICE_TYPE}",
            port=_ANNOUNCED_PORT,
            properties=_PROPERTIES,
            parsed_addresses=[bind_host],
        )

    async def start(self) -> bool:
        """Announce the service. Returns True on success."""
        aiozc = AsyncZeroconf(interfaces=[self._bind_host])
        try:
            # cooperating_responders=True: a system avahi-daemon may already
            # be running on the host: coexist rather than treat that as a
            # conflict.
            await aiozc.async_register_service(self._info, cooperating_responders=True)
        except Exception:
            logger.exception("mDNS announcement failed to start on %s", self._bind_host)
            await aiozc.async_close()
            return False
        self._aiozc = aiozc
        logger.info(
            "mDNS announcement started: %s on %s (%s)",
            _SERVICE_NAME,
            self._bind_host,
            _SERVICE_TYPE,
        )
        return True

    async def stop(self):
        """Withdraw the announcement and release the socket."""
        if self._aiozc is not None:
            await self._aiozc.async_unregister_service(self._info)
            await self._aiozc.async_close()
            self._aiozc = None
            logger.info("mDNS announcement stopped")

    def is_running(self) -> bool:
        return self._aiozc is not None
