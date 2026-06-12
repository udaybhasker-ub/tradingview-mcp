from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import tradingview_mcp.server as tv_server

# Build permissive security settings
security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

# Patch the security onto the already-constructed mcp instance
tv_server.mcp._settings.transport_security = security

tv_server.mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
