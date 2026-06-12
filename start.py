from mcp.server.transport_security import TransportSecuritySettings
import tradingview_mcp.server as tv_server

tv_server.mcp.settings.host = "0.0.0.0"
tv_server.mcp.settings.port = 8000
tv_server.mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

tv_server.mcp.run(transport="streamable-http")
