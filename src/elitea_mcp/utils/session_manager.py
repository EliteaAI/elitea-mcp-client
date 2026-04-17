import asyncio
import atexit
import threading
from typing import Dict, Optional, Tuple, Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ..config import load_config


class SessionManager:
    """Manages persistent MCP sessions for stateful servers."""
    
    def __init__(self):
        self._sessions: Dict[str, Tuple[Any, Any, ClientSession]] = {}
        self._lock = threading.Lock()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_running = threading.Event()
        # Register cleanup on exit
        atexit.register(self.cleanup_all)
    
    def _ensure_event_loop(self):
        """Ensure we have a dedicated event loop running in a separate thread."""
        with self._lock:
            if self._event_loop is None or self._event_loop.is_closed():
                if self._loop_thread is not None and self._loop_thread.is_alive():
                    return
                
                self._loop_running.clear()
                
                def run_loop():
                    self._event_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self._event_loop)
                    self._loop_running.set()
                    try:
                        self._event_loop.run_forever()
                    finally:
                        self._event_loop.close()
                
                self._loop_thread = threading.Thread(target=run_loop, daemon=True)
                self._loop_thread.start()
                
                # Wait for loop to be ready
                self._loop_running.wait(timeout=5.0)
    
    def _run_in_loop(self, coro):
        """Run a coroutine in the managed event loop."""
        self._ensure_event_loop()
        if self._event_loop is None:
            raise RuntimeError("Event loop not available")
        
        config = load_config()
        future = asyncio.run_coroutine_threadsafe(coro, self._event_loop)
        return future.result(timeout=config['timeout'])
    
    def get_session_sync(self, server_name: str, server_conf: dict) -> ClientSession:
        """Synchronous wrapper for get_session."""
        return self._run_in_loop(self.get_session(server_name, server_conf))
    
    def cleanup_session_sync(self, server_name: str):
        """Synchronous wrapper for cleanup_session."""
        return self._run_in_loop(self.cleanup_session(server_name))
    
    def call_tool_with_recovery_sync(self, server_name: str, server_conf: dict, params: dict) -> str:
        """Synchronous wrapper for call_tool_with_recovery."""
        return self._run_in_loop(self.call_tool_with_recovery(server_name, server_conf, params))

    async def get_session(self, server_name: str, server_conf: dict) -> ClientSession:
        """Get or create a session for the given server."""
        with self._lock:
            if server_name in self._sessions:
                # Return existing session
                _, _, session = self._sessions[server_name]
                print(f"Reusing existing session for server: {server_name}")
                return session
            
            # Create new session
            try:
                if server_conf.get("type", "stdio").lower() == "stdio":
                    server_parameters = StdioServerParameters(**server_conf)
                    stdio_ctx = stdio_client(server_parameters)
                    read, write = await stdio_ctx.__aenter__()
                    
                    session_ctx = ClientSession(read, write)
                    session = await session_ctx.__aenter__()
                    await session.initialize()
                    
                    # Store the context managers and session
                    self._sessions[server_name] = (stdio_ctx, session_ctx, session)
                    print(f"Created persistent session for stdio server: {server_name}")

                elif server_conf["type"].lower() == "http":
                    http_ctx = streamablehttp_client(server_conf["url"], server_conf.get("headers", {}))
                    read_stream, write_stream, _ = await http_ctx.__aenter__()

                    session_ctx = ClientSession(read_stream, write_stream)
                    session = await session_ctx.__aenter__()
                    await session.initialize()

                    # Store the context managers and session
                    self._sessions[server_name] = (http_ctx, session_ctx, session)
                    print(f"Created persistent session for HTTP server: {server_name}")
                    
                elif server_conf["type"].lower() == "sse":
                    sse_ctx = sse_client(server_conf["url"], server_conf.get("headers", {}))
                    streams = await sse_ctx.__aenter__()
                    
                    session_ctx = ClientSession(*streams)
                    session = await session_ctx.__aenter__()
                    await session.initialize()
                    
                    # Store the context managers and session
                    self._sessions[server_name] = (sse_ctx, session_ctx, session)
                    print(f"Created persistent session for SSE server: {server_name}")
                else:
                    raise ValueError(f"Unsupported server type: {server_conf['type']}")
                
                return self._sessions[server_name][2]
            except Exception as e:
                print(f"Failed to create session for {server_name}: {e}")
                # Clean up any partial state
                if server_name in self._sessions:
                    del self._sessions[server_name]
                raise
    
    async def cleanup_session(self, server_name: str):
        """Clean up a specific session."""
        with self._lock:
            if server_name in self._sessions:
                ctx1, ctx2, session = self._sessions[server_name]
                
                try:
                    # Clean up session context
                    if hasattr(ctx2, '__aexit__'):
                        await ctx2.__aexit__(None, None, None)
                    
                    # Clean up stdio/sse context
                    if hasattr(ctx1, '__aexit__'):
                        await ctx1.__aexit__(None, None, None)
                    
                    print(f"Cleaned up session for server: {server_name}")
                except Exception as e:
                    print(f"Error cleaning up session for {server_name}: {e}")
                
                del self._sessions[server_name]
    
    def cleanup_all(self):
        """Clean up all sessions (synchronous for atexit)."""
        if not self._sessions:
            return
            
        print("Cleaning up all persistent sessions...")
        try:
            # Use our dedicated event loop for cleanup
            if self._event_loop and not self._event_loop.is_closed():
                # Run cleanup in our dedicated event loop
                future = asyncio.run_coroutine_threadsafe(
                    self._cleanup_all_async(), 
                    self._event_loop
                )
                future.result(timeout=10.0)  # 10 second timeout for cleanup
                
                # Stop the event loop
                self._event_loop.call_soon_threadsafe(self._event_loop.stop)
                if self._loop_thread and self._loop_thread.is_alive():
                    self._loop_thread.join(timeout=5.0)
            else:
                # Fallback: create temporary event loop for cleanup
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self._cleanup_all_async())
                    loop.close()
                except Exception as e:
                    print(f"Error during fallback cleanup: {e}")
        except Exception as e:
            print(f"Error during cleanup: {e}")
        finally:
            self._sessions.clear()
    
    async def _cleanup_all_async(self):
        """Async version of cleanup_all."""
        tasks = []
        for server_name in list(self._sessions.keys()):
            tasks.append(self.cleanup_session(server_name))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    def is_stateful(self, server_conf: dict) -> bool:
        """Check if a server is configured for stateful operation."""
        return server_conf.get("stateful", False)
    
    def list_active_sessions(self) -> Dict[str, str]:
        """List all active sessions with their server types."""
        return {
            name: "stdio" if hasattr(data[0], 'command') else "sse" 
            for name, data in self._sessions.items()
        }
    
    async def reset_session(self, server_name: str, server_conf: dict) -> Optional[ClientSession]:
        """Reset a specific session by closing and recreating it."""
        await self.cleanup_session(server_name)
        return await self.get_session(server_name, server_conf)
    
    async def call_tool_with_recovery(self, server_name: str, server_conf: dict, params: dict) -> str:
        """Call a tool with automatic session recovery on failure."""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                session = await self.get_session(server_name, server_conf)
                tool_result = await session.call_tool(params["name"], params["arguments"])
                return tool_result.content[0].text
            except Exception as e:
                print(f"Error calling tool on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    print(f"Recreating session for {server_name}...")
                    await self.cleanup_session(server_name)
                else:
                    raise e


# Global session manager instance
_session_manager = SessionManager()


def get_session_manager() -> SessionManager:
    """Get the global session manager instance."""
    return _session_manager
