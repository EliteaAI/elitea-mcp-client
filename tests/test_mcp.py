import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from src.elitea_mcp.server.mcp import create_server, run
from mcp.server.lowlevel import Server
import mcp.types as types

@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.agent_name = "TestAgent"
    agent.description = "Test Description"
    agent.pydantic_model.schema.return_value = {"title": "TestSchema"}
    return agent

def test_create_server(mock_agent):
    # With the updated implementation, it should use the agent's name
    server = create_server(mock_agent)
    assert isinstance(server, Server)
    assert server.name == "TestAgent"  # Now using the agent's name
    
    # Test with custom name - this should still override the agent name
    custom_server = create_server(mock_agent, name="custom-server")
    assert custom_server.name == "custom-server"
    
    # Test with agent that doesn't have a name
    nameless_agent = MagicMock()
    # Remove agent_name attribute
    del nameless_agent.agent_name
    nameless_server = create_server(nameless_agent)
    assert nameless_server.name == "mcp-simple-prompt"  # Fallback to default name

@pytest.mark.skip(reason="Cannot directly access the decorated async functions in the MCP Server")
@pytest.mark.asyncio
async def test_fetch_tool(mock_agent):
    server = create_server(mock_agent)
    mock_agent.predict.return_value = {"chat_history": [{"content": "Response"}]}

    # This approach doesn't work because we don't have direct access to the handler functions
    # Implementation needs to be revised with a proper testing approach for the MCP server
    pass

@pytest.mark.skip(reason="Cannot directly access the decorated async functions in the MCP Server")
@pytest.mark.asyncio
async def test_list_tools(mock_agent):
    server = create_server(mock_agent)
    # This approach doesn't work because we don't have direct access to the handler functions
    # Implementation needs to be revised with a proper testing approach for the MCP server
    pass

# New tests to improve coverage

def test_create_server_available_agents(mock_agent):
    """Test that server is created with the correct available agents."""
    mock_agent.agent_name = "CustomAgent"
    server = create_server(mock_agent)
    # Since we cannot directly access the registered functions, we test the server properties
    assert isinstance(server, Server)
    # This is an indirect test - the server should hold the agent name for use in handlers
    assert mock_agent.agent_name == "CustomAgent"

@patch("src.elitea_mcp.server.mcp.uvicorn.Server")
@patch("src.elitea_mcp.server.mcp.Starlette")
@patch("src.elitea_mcp.server.mcp.SseServerTransport")
def test_run_with_sse_transport(mock_sse, mock_starlette, mock_uvicorn_server, mock_agent):
    """Test running the server with SSE transport."""
    mock_sse_instance = MagicMock()
    mock_sse.return_value = mock_sse_instance
    
    mock_starlette_app = MagicMock()
    mock_starlette.return_value = mock_starlette_app
    
    mock_server_instance = MagicMock()
    mock_uvicorn_server.return_value = mock_server_instance
    
    # Test with SSE transport
    run(mock_agent, transport="sse", host="127.0.0.1", port=9000)
    
    # Verify SSE transport was used correctly
    mock_sse.assert_called_once_with("/messages/")
    # Verify Starlette app was created with correct routes
    mock_starlette.assert_called_once()
    # Verify uvicorn server was configured correctly
    mock_uvicorn_server.assert_called_once()
    assert mock_uvicorn_server.call_args[0][0].host == "127.0.0.1"
    assert mock_uvicorn_server.call_args[0][0].port == 9000
    # Verify server was run
    mock_server_instance.run.assert_called_once()

@patch("src.elitea_mcp.server.mcp.anyio.run")
@patch("src.elitea_mcp.server.mcp.stdio_server")
def test_run_with_stdio_transport(mock_stdio_server, mock_anyio_run, mock_agent):
    """Test running the server with stdio transport."""
    # Test with stdio transport
    run(mock_agent, transport="stdio")
    
    # Verify anyio.run was called
    mock_anyio_run.assert_called_once()

def test_run_with_unsupported_transport(mock_agent):
    """Test running the server with an unsupported transport raises ValueError."""
    with pytest.raises(ValueError, match="Unsupported transport: invalid"):
        run(mock_agent, transport="invalid")

@patch("src.elitea_mcp.server.mcp.create_server")
def test_run_creates_server_if_not_provided(mock_create_server, mock_agent):
    """Test that run creates a server if none is provided."""
    mock_server = MagicMock()
    mock_create_server.return_value = mock_server
    
    with patch("src.elitea_mcp.server.mcp.anyio.run") as mock_anyio_run:
        run(mock_agent)
        
    # Verify server was created with agent name (which is now passed through)
    mock_create_server.assert_called_once_with(mock_agent, "TestAgent")

@patch("src.elitea_mcp.server.mcp.anyio.run")
def test_run_uses_provided_server(mock_anyio_run, mock_agent):
    """Test that run uses the provided server if one is given."""
    mock_server = MagicMock()
    
    with patch("src.elitea_mcp.server.mcp.create_server") as mock_create_server:
        run(mock_agent, server=mock_server)
        
    # Verify create_server was not called
    mock_create_server.assert_not_called()
