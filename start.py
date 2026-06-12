from mcp.server import transport_security
from tradingview_mcp.server import mcp

# Patch the validation function directly
transport_security.TransportSecuritySettings.is_host_allowed = lambda self, host: True

mcp.settings.host = "0.0.0.0"
mcp.settings.port = 8000

mcp.run(transport="streamable-http")
