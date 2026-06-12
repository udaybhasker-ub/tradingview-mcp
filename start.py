# start.py
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import importlib
import sys

# Import the existing server's mcp instance
from tradingview_mcp.server import mcp

# Override transport security to allow all hosts
mcp._transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)

mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
