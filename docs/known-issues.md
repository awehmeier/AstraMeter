# Known Issues (non-blocking)

## mypy: aiohttp ClientWebSocketResponse generic mismatch
- Files: src/astrameter/powermeter/homewizard.py:137,141;
  src/astrameter/powermeter/homeassistant.py:139
- Cause: bare `aiohttp.ClientWebSocketResponse` annotation defaults to
  `Literal[True]`; `ws_connect(...)` calls here return the generic
  `ClientWebSocketResponse[bool]` overload (no literal kwarg passed).
  Cosmetic type-stub mismatch, no runtime impact.
- Fix idea: annotate params as `aiohttp.ClientWebSocketResponse[bool]`.
- Not fixed here: out of scope for the Modbus TCP output server change.
