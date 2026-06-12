from mcp.server import transport_security
from tradingview_mcp.server import mcp

# Patch at the function level
original_handle = transport_security.TransportSecuritySettings.is_host_allowed
transport_security.TransportSecuritySettings.is_host_allowed = lambda self, host: True

mcp.settings.host = "0.0.0.0"
mcp.settings.port = 8000

mcp.run(transport="streamable-http")
