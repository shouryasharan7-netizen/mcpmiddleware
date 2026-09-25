from mcp.server.mcpserver import MCPServer
mcp = MCPServer("reference")
@mcp.tool()
def echo(text: str) -> str:
    """Return the provided text."""
    return text
if __name__ == '__main__':
    mcp.run(transport='streamable-http')
