import asyncio
import atexit
import logging
import threading
from contextlib import AsyncExitStack
from typing import Dict, Optional, Tuple, Any, Union

from mcp import ClientSession, types

from ..config_model import DEFAULT_STARTUP_TIMEOUT, DEFAULT_TIMEOUT
from .server_loader import open_session, read_startup_timeout, read_transport
from .server_output import ServerOutputPipe
from .tool_result import render_tool_result

TEARDOWN_MARGIN = 5
MAX_CALL_ATTEMPTS = 2

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages persistent MCP sessions for stateful servers."""
    
    def __init__(self):
        self._sessions: Dict[str, Tuple[Any, Any, ClientSession]] = {}
        self._lock = threading.Lock()
        self._event_loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None
        self._loop_running = threading.Event()
        self.timeout = DEFAULT_TIMEOUT
        self.startup_timeout = DEFAULT_STARTUP_TIMEOUT
        self.on_tools_changed = None
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
    
    def _run_in_loop(self, coro, timeout=None):
        """Run a coroutine in the managed event loop, waiting at most `timeout` seconds (the tool timeout by default)."""
        self._ensure_event_loop()
        if self._event_loop is None:
            raise RuntimeError("Event loop not available")
        
        future = asyncio.run_coroutine_threadsafe(coro, self._event_loop)
        return future.result(timeout=timeout or self.timeout)
    
    def get_session_sync(self, server_name: str, server_conf: dict) -> ClientSession:
        """Synchronous wrapper for get_session."""
        return self._run_in_loop(self.get_session(server_name, server_conf))
    
    def cleanup_session_sync(self, server_name: str):
        """Synchronous wrapper for cleanup_session."""
        return self._run_in_loop(self.cleanup_session(server_name))
    
    def call_tool_with_recovery_sync(self, server_name: str, server_conf: dict, params: dict) -> Union[str, dict]:
        """Synchronous wrapper for call_tool_with_recovery."""
        return self._run_in_loop(self.call_tool_with_recovery(server_name, server_conf, params),
                                 timeout=self.calculate_stateful_call_budget(server_conf))

    def calculate_stateless_call_budget(self, server_conf: dict) -> int:
        """Seconds a call may take when the server is started for it: the start, the call and the SDK's teardown."""
        return read_startup_timeout(server_conf, self.startup_timeout) + self.timeout + TEARDOWN_MARGIN

    def calculate_stateful_call_budget(self, server_conf: dict) -> int:
        return MAX_CALL_ATTEMPTS * (read_startup_timeout(server_conf, self.startup_timeout) + TEARDOWN_MARGIN) + self.timeout

    def calculate_call_budget(self, server_conf: dict) -> int:
        """The longest this client works on one call to the server, which the platform must wait for."""
        if self.is_stateful(server_conf):
            return self.calculate_stateful_call_budget(server_conf)
        return self.calculate_stateless_call_budget(server_conf)

    async def get_session(self, server_name: str, server_conf: dict) -> ClientSession:
        """Get or create a session for the given server."""
        with self._lock:
            if server_name in self._sessions:
                # Return existing session
                _, _, session = self._sessions[server_name]
                logger.debug(f"Reusing existing session for server: {server_name}")
                return session
            
            stack = AsyncExitStack()
            output = ServerOutputPipe(server_name) if read_transport(server_conf) == "stdio" else None
            try:
                session = await stack.enter_async_context(
                    open_session(server_conf, read_startup_timeout(server_conf, self.startup_timeout),
                                 errlog=output.writer if output else None,
                                 message_handler=self.create_message_handler(server_name)))
            except Exception as e:
                logger.warning(f"Failed to create session for {server_name}: {e}")
                await stack.aclose()
                raise
            finally:
                if output:
                    output.close_writer()
            self._sessions[server_name] = (stack, None, session)
            logger.debug(f"Created persistent session for {server_conf.get('type', 'stdio')} server: {server_name}")
            return session

    def create_message_handler(self, server_name: str):
        """Re-list a stateful server's tools when it announces a change, outside the session's receive loop."""
        async def handle_message(message):
            if isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification):
                asyncio.get_running_loop().create_task(self.report_tools(server_name))
        return handle_message

    async def report_tools(self, server_name: str):
        session_entry = self._sessions.get(server_name)
        if session_entry is None or self.on_tools_changed is None:
            return
        try:
            tools = await asyncio.wait_for(session_entry[2].list_tools(), self.timeout)
        except Exception as e:
            logger.warning(f"Could not list the changed tools of {server_name}: {e}")
            return
        self.on_tools_changed(server_name, [tool.model_dump() for tool in tools.tools])

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
                    
                    logger.debug(f"Cleaned up session for server: {server_name}")
                except Exception as e:
                    logger.warning(f"Error cleaning up session for {server_name}: {e}")
                
                del self._sessions[server_name]
    
    def cleanup_all(self):
        """Clean up all sessions (synchronous for atexit)."""
        if not self._sessions:
            return
            
        logger.debug("Cleaning up all persistent sessions...")
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
                    logger.warning(f"Error during fallback cleanup: {e}")
        except Exception as e:
            logger.warning(f"Error during cleanup: {e}")
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
    
    async def call_tool_with_recovery(self, server_name: str, server_conf: dict, params: dict) -> Union[str, dict]:
        """Call a tool with automatic session recovery on failure."""
        max_retries = MAX_CALL_ATTEMPTS
        for attempt in range(max_retries):
            try:
                session = await self.get_session(server_name, server_conf)
                tool_result = await asyncio.wait_for(session.call_tool(params["name"], params["arguments"]), self.timeout)
                return render_tool_result(tool_result)
            except TimeoutError:
                raise
            except Exception as e:
                logger.warning(f"Error calling tool on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    logger.info(f"Recreating session for {server_name}...")
                    await self.cleanup_session(server_name)
                else:
                    raise e


# Global session manager instance
_session_manager = SessionManager()


def get_session_manager() -> SessionManager:
    """Get the global session manager instance."""
    return _session_manager
