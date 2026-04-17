import anyio
import logging
from typing import Any, List, Union
import mcp.types as types
from mcp.server.lowlevel import Server
import sys
import io
import contextlib

# Handle SSE transport directly without using asyncio.run()
import uvicorn
from uvicorn.config import Config

# Create Starlette app synchronously
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from mcp.server.stdio import stdio_server

from elitea_mcp.utils.name import build_agent_identifier

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def suppress_stdout():
    """Context manager to suppress stdout during agent prediction when using stdio transport."""
    original_stdout = sys.stdout
    try:
        # Redirect stdout to a string buffer to capture any unwanted output
        sys.stdout = io.StringIO()
        yield
    finally:
        # Restore original stdout
        sys.stdout = original_stdout

def extract_final_response(response: dict) -> str:
    """
    Extract the final assistant response from the agent's response object.
    This should return ONLY the last assistant message content.
    
    Args:
        response: The full response object from the agent
        
    Returns:
        The clean text content of the final assistant response
    """
    # Handle error cases first
    if response.get('error'):
        return f"Error: {response['error']}"
    
    # Primary method: Get the last assistant message from chat_history
    chat_history = response.get('chat_history', [])
    if chat_history and isinstance(chat_history, list):
        # Find the last assistant message (iterate in reverse)
        for message in reversed(chat_history):
            if isinstance(message, dict) and message.get('role') == 'assistant':
                content = message.get('content', '')
                if content and isinstance(content, str):
                    # This is the final assistant message - return it
                    return content.strip()
    
    # Fallback 1: Check if there's a direct response in thinking_steps
    thinking_steps = response.get('thinking_steps', [])
    if thinking_steps and isinstance(thinking_steps, list):
        # Look for the last step that contains actual response content
        for step in reversed(thinking_steps):
            if isinstance(step, dict):
                # Check for message content in the step
                if 'message' in step and isinstance(step['message'], dict):
                    message_content = step['message'].get('content', '')
                    if message_content and isinstance(message_content, str) and len(message_content.strip()) > 0:
                        return message_content.strip()
                
                # Check for direct text content in the step
                text_content = step.get('text', '')
                if text_content and isinstance(text_content, str) and len(text_content.strip()) > 0:
                    return text_content.strip()
    
    # Fallback 2: Check if there's any content in the response structure
    if isinstance(response, dict):
        # Look for any 'content' field at the top level
        if 'content' in response and isinstance(response['content'], str):
            return response['content'].strip()
        
        # Look for any 'text' field at the top level
        if 'text' in response and isinstance(response['text'], str):
            return response['text'].strip()
    
    # Final fallback: return a generic message indicating no content was found
    return "No assistant response found in the agent output"


def create_server(agent: Union[Any, List[Any]], name=None):
    """Create and return an MCP server instance.
    
    Args:
        agent: Single agent or list of agents to serve
        name: Optional server name (defaults to agent name for single agents)
    """
    # Use agent name as server name if not specified and if agent has a name
    if name is None:
        if not isinstance(agent, list) and hasattr(agent, 'agent_name'):
            name = agent.agent_name
        else:
            name = "mcp-simple-prompt"
    
    app = Server(name)
    
    # Convert single agent to a list for uniform handling
    agents = agent if isinstance(agent, list) else [agent]
    
    # Create a dictionary of available agents by name
    available_agents = {}
    for a in agents:
        if hasattr(a, 'agent_name'):
            identifier = build_agent_identifier(a.agent_name)
            if identifier in available_agents:
                logger.warning(
                    "Skipping agent with colliding name: %s -> %s",
                    a.agent_name, identifier,
                )
                continue
            available_agents[identifier] = a
        else:
            # Fallback for agents without a name (assign index-based name)
            agent_index = len(available_agents)
            available_agents[f"agent_{agent_index}"] = a
    
    @app.call_tool()
    async def fetch_tool(
        name: str, arguments: dict
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        if name not in available_agents:
            raise ValueError(f"Tool '{name}' not found")
        if "user_input" not in arguments:
            raise ValueError("Missing required argument 'user_input'")
            
        # Get the correct agent by name
        current_agent = available_agents[name]
        
        # Suppress stdout during agent prediction to prevent interference with stdio transport
        with suppress_stdout():
            response = current_agent.predict(**arguments)
        
        # Debug logging for troubleshooting response extraction
        # To enable: set environment variable ELITEA_MCP_DEBUG=1
        logger.debug("=== COMPLETE AGENT RESPONSE ===")
        logger.debug(f"Response type: {type(response)}")
        if isinstance(response, dict):
            logger.debug(f"Response keys: {list(response.keys())}")
            
            # Log chat_history in detail
            if 'chat_history' in response:
                chat_history = response['chat_history']
                logger.debug(f"Chat history type: {type(chat_history)}")
                logger.debug(f"Chat history length: {len(chat_history) if isinstance(chat_history, list) else 'N/A'}")
                if isinstance(chat_history, list):
                    for i, msg in enumerate(chat_history):
                        logger.debug(f"Message {i}: {type(msg)} - {msg}")
            
            # Log thinking_steps if present
            if 'thinking_steps' in response:
                thinking_steps = response['thinking_steps']
                logger.debug(f"Thinking steps type: {type(thinking_steps)}")
                logger.debug(f"Thinking steps length: {len(thinking_steps) if isinstance(thinking_steps, list) else 'N/A'}")
                if isinstance(thinking_steps, list) and thinking_steps:
                    logger.debug(f"Last thinking step: {thinking_steps[-1]}")
            
            # Log error if present
            if 'error' in response:
                logger.debug(f"Error in response: {response['error']}")
        else:
            logger.debug(f"Full response: {response}")
        logger.debug("=== END AGENT RESPONSE ===")
        
        # Extract clean response text
        clean_response = extract_final_response(response)
        logger.debug(f"=== EXTRACTED RESPONSE ===")
        logger.debug(f"Clean response: {clean_response}")
        logger.debug(f"Clean response length: {len(clean_response)}")
        logger.debug("=== END EXTRACTED RESPONSE ===")
        
        return [types.TextContent(type="text", text=clean_response)]

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        tools = []
        for agent_name, agent_obj in available_agents.items():
            # Get description and schema safely
            description = getattr(agent_obj, 'description', f"Agent {agent_name}")
            
            # Handle agents that might not have pydantic_model attribute
            if hasattr(agent_obj, 'pydantic_model') and hasattr(agent_obj.pydantic_model, 'schema'):
                schema = agent_obj.pydantic_model.schema()
            else:
                schema = {"title": agent_name, "type": "object", "properties": {"user_input": {"type": "string"}}}
            
            tools.append(
                types.Tool(
                    name=agent_name,
                    description=description,
                    inputSchema=schema
                )
            )
        return tools
        
    return app


def run(agent: Any, server=None, transport="stdio", host='0.0.0.0', port=8000):
    """Run the MCP server.
    
    Args:
        agent: The agent or list of agents to serve
        server: Optional pre-configured server (will create one if not provided)
        transport: Transport mechanism ('stdio' or 'sse')
        host: Host to bind to when using SSE transport
        port: Port to listen on when using SSE transport
    """
    # Create server if not provided
    if server is None:
        # Get agent name if available
        name = agent.agent_name if hasattr(agent, 'agent_name') else None
        app = create_server(agent, name)
    else:
        app = server
    
    if transport.lower() == "sse":
        # Set up SSE transport
        sse = SseServerTransport("/messages/")

        async def handle_sse(request):
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )

        starlette_app = Starlette(
            debug=True,
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )
        
        # Run with uvicorn directly
        config = Config(
            app=starlette_app,
            host=host,
            port=port,
            timeout_graceful_shutdown=5,
        )
        
        logger.debug(f"Starting MCP server with SSE transport on {host}:{port}")
        server = uvicorn.Server(config)
        server.run()
    elif transport.lower() == "stdio":
        logger.debug("Starting MCP server with stdio transport")
        async def arun():
            async with stdio_server() as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )
        anyio.run(arun)
    else:
        raise ValueError(f"Unsupported transport: {transport}")