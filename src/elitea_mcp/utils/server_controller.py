"""
Server controller for managing MCP servers from the tray application.
"""
import asyncio
import multiprocessing
import os
import platform
import subprocess
import sys
import threading
import time
from typing import Optional, Callable

# Fix for macOS fork safety with GUI applications
if platform.system() == "Darwin":
    # Set environment variable to disable fork safety check as a fallback
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    
    # Set multiprocessing start method to spawn to avoid fork issues
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        # Already set, ignore
        pass

from ..config import load_config
from ..utils.sio import start_socket_connection, get_all_tools

SERVER_RESTART_DELAY = 2  # Delay before restarting servers to allow resources to release
MONITOR_DELAY = 5  # Delay for monitoring daemon process status

class ServerController:
    """Controller for managing MCP server lifecycle."""
    
    def __init__(self):
        self.is_running = False
        self.server_thread: Optional[threading.Thread] = None
        self.server_process: Optional[subprocess.Popen] = None
        self.stop_event = threading.Event()
        self.status_callback: Optional[Callable[[str], None]] = None
        self.error_callback: Optional[Callable[[str], None]] = None
        self.daemon_mode = False
        
    def set_status_callback(self, callback: Callable[[str], None]):
        """Set callback for status updates."""
        self.status_callback = callback
        
    def set_error_callback(self, callback: Callable[[str], None]):
        """Set callback for error notifications."""
        self.error_callback = callback
        
    def _notify_status(self, message: str):
        """Notify status change."""
        if self.status_callback:
            self.status_callback(message)
            
    def _notify_error(self, message: str):
        """Notify error."""
        if self.error_callback:
            self.error_callback(message)
    
    def start_servers(self, daemon_mode: bool = False) -> bool:
        """Start MCP servers. Returns True if successful.
        
        Args:
            daemon_mode: If True, start as separate daemon process. If False, start in current process.
        """
        if self.is_running:
            return True
            
        config = load_config()
        servers = config.get("servers", {})
        
        if not servers:
            self._notify_error("No servers configured. Please bootstrap servers first.")
            return False
            
        self.daemon_mode = daemon_mode
        
        if daemon_mode:
            return self._start_daemon_servers()
        else:
            return self._start_embedded_servers()

    def restart_servers(self) -> bool:
        """Restart MCP servers using the current daemon mode."""
        current_mode = self.daemon_mode
        if self.is_running:
            self.stop_servers()
            # Give a moment for resources to be released
            time.sleep(SERVER_RESTART_DELAY)
        return self.start_servers(daemon_mode=current_mode)
    
    def _start_daemon_servers(self) -> bool:
        """Start MCP servers as a separate daemon process."""
        try:
            # Find the elitea-mcp command
            elitea_mcp_cmd = None
            
            # Try to find elitea-mcp in PATH
            import shutil
            elitea_mcp_cmd = shutil.which('elitea-mcp')
            
            if not elitea_mcp_cmd:
                # Fallback: try to run via Python module
                python_cmd = sys.executable
                cmd = [python_cmd, '-m', 'elitea_mcp.main', 'serve', '--daemon']
            else:
                cmd = [elitea_mcp_cmd, 'serve', '--daemon']
            
            self._notify_status(f"Starting MCP servers in daemon mode...")
            
            # Create environment for subprocess
            env = os.environ.copy()
            
            # On macOS, ensure fork safety for subprocesses
            if platform.system() == "Darwin":
                env['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
            
            # Start the process with safer settings for macOS
            if platform.system() == "Darwin":
                # Use posix_spawn instead of fork on macOS
                self.server_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    env=env,
                    start_new_session=True,  # Start in new session to avoid parent issues
                    preexec_fn=None  # Don't use preexec_fn on macOS
                )
            else:
                self.server_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.PIPE,
                    env=env
                )
            
            # Give it a moment to start
            time.sleep(SERVER_RESTART_DELAY)
            
            # Check if process is still running
            if self.server_process.poll() is None:
                self.is_running = True
                self._notify_status("MCP servers started in daemon mode")
                
                # Start a thread to monitor the process
                self._start_process_monitor()
                return True
            else:
                # Process failed to start
                stdout, stderr = self.server_process.communicate()
                error_msg = stderr.decode() if stderr else "Unknown error"
                self._notify_error(f"Failed to start daemon servers: {error_msg}")
                return False
                
        except Exception as e:
            self._notify_error(f"Error starting daemon servers: {str(e)}")
            return False
    
    def _start_embedded_servers(self) -> bool:
        """Start MCP servers in the current process (embedded mode)."""
        def run_server_loop():
            try:
                self.is_running = True
                self._notify_status("Starting MCP servers...")
                
                # Get all tools from configured servers
                config = load_config()
                servers = config.get("servers", {})
                all_tools = asyncio.run(get_all_tools(servers))
                
                if not all_tools:
                    self._notify_error("No tools available from configured servers.")
                    return
                    
                server_names = [server["name"] for server in all_tools]
                self._notify_status(f"Connected to servers: {', '.join(server_names)}")
                
                # Start socket connection
                self._notify_status("Starting socket connection...")
                sio = start_socket_connection(config, all_tools, self._notify_status, self._notify_error)
                
                # Keep the server running until stop is requested
                while not self.stop_event.is_set():
                    time.sleep(SERVER_RESTART_DELAY)
                
                sio.disconnect()
                    
            except Exception as e:
                self._notify_error(f"Server error: {str(e)}")
            finally:
                self.is_running = False
                self._notify_status("MCP servers stopped")
                
        self.stop_event.clear()
        self.server_thread = threading.Thread(target=run_server_loop, daemon=True)
        self.server_thread.start()
        return True
    
    def _start_process_monitor(self):
        """Start a thread to monitor the daemon process."""
        def monitor_process():
            while self.is_running and self.server_process:
                # Check if process is still running
                if self.server_process.poll() is not None:
                    # Process has terminated
                    self.is_running = False
                    self._notify_status("Daemon MCP servers stopped")
                    break
                time.sleep(MONITOR_DELAY)  # Check every 5 seconds
        
        monitor_thread = threading.Thread(target=monitor_process, daemon=True)
        monitor_thread.start()
    
    def stop_servers(self):
        """Stop MCP servers."""
        if not self.is_running:
            return
            
        self._notify_status("Stopping MCP servers...")
        
        if self.daemon_mode and self.server_process:
            # Stop daemon process
            try:
                self.server_process.terminate()
                
                # Give it a moment to terminate gracefully
                try:
                    self.server_process.wait(timeout=MONITOR_DELAY)
                except subprocess.TimeoutExpired:
                    # Force kill if it doesn't terminate
                    self.server_process.kill()
                    self.server_process.wait()
                    
                self.server_process = None
            except Exception as e:
                self._notify_error(f"Error stopping daemon servers: {str(e)}")
        else:
            # Stop embedded servers
            self.stop_event.set()
            
            if self.server_thread and self.server_thread.is_alive():
                # Give it a few seconds to stop gracefully
                self.server_thread.join(timeout=MONITOR_DELAY)
            
        self.is_running = False
        self.daemon_mode = False
        self._notify_status("MCP servers stopped")
    
    def get_server_status(self) -> dict:
        """Get current server status."""
        config = load_config()
        servers = config.get("servers", {})
        
        return {
            "is_running": self.is_running,
            "daemon_mode": self.daemon_mode,
            "configured_servers": list(servers.keys()),
            "server_count": len(servers),
            "process_id": self.server_process.pid if self.server_process else None
        }


# Global server controller instance
_server_controller = None

def get_server_controller() -> ServerController:
    """Get the global server controller instance."""
    global _server_controller
    if _server_controller is None:
        _server_controller = ServerController()
    return _server_controller
