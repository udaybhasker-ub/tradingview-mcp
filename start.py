from mcp.server.transport_security import TransportSecuritySettings
from tradingview_mcp.server import mcp

mcp.settings.host = "0.0.0.0"
mcp.settings.port = 8000
mcp._transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

mcp.run(transport="streamable-http")
