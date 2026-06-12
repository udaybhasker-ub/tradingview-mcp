from mcp.server.transport_security import TransportSecuritySettings
import tradingview_mcp.server as tv_server

security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

tv_server.mcp.settings.transport_security = security

tv_server.mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
