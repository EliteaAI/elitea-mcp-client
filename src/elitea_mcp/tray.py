#!/usr/bin/env python3
"""
System tray application for EliteA MCP Client.
Provides easy access to configuration and server management.
"""
import functools
import json
import os
import sys
import threading
import subprocess
import platform
import multiprocessing
import logging

logger = logging.getLogger(__name__)

# Fix for macOS fork safety with GUI applications
if platform.system() == "Darwin":
    # Set environment variable to disable fork safety check as a fallback
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    
    # Set multiprocessing start method to spawn to avoid fork issues
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        # Already set, ignore
        logger.debug("Multiprocessing start method already set to 'spawn'")
        pass

import pystray
from PIL import Image, ImageDraw
from .config_errors import ConfigError
from .config import (SCHEMA_REFERENCE, get_bootstrap_config, set_bootstrap_config, interactive_bootstrap,
                     get_config_file, load_config)
from .config_model import NOTIFICATION_LIMIT, check_config, write_config_schema
from .utils.server_controller import get_server_controller
from .utils.diagnostics import REPORT_FILE_NAME, TRAY_DIAGNOSTICS_HINT, DoctorContext, format_report, run_checks, summarize
from .utils.server_registry import DECLINED, FAILED
from .main import main, setup_file_logger, get_default_paths
from .utils.package_updates import DEFAULT_CHECK_INTERVAL_HOURS, UpdateChecker, get_installed_version
from .utils.self_update import consume_update_result, resolve_upgrade_plan, start_update_helper

STATUS_DOT_COLORS = {
    "config_error": (229, 57, 53, 255),
    "running": (76, 175, 80, 255),
}

CLIPBOARD_COMMANDS = {
    "darwin": [["pbcopy"]],
    "windows": [["clip"]],
    "linux": [["wl-copy"], ["xclip", "-selection", "clipboard"]],
}


class MCPTrayApp:
    def __init__(self):
        self.icon = None
        self.server_controller = get_server_controller()
        self.is_server_running = False
        self.config_error = None
        self.config_report = None
        self.settings = None
        self.diagnostics_running = False
        self._read_config()
        config = self.settings
        self.update_checker = (
            UpdateChecker(self.on_update_state_change,
                          config.get("update_check_interval_hours", DEFAULT_CHECK_INTERVAL_HOURS))
            if config and config.get("update_check", True) else None
        )
        
        # Set up callbacks for server controller
        self.server_controller.set_status_callback(self.on_server_status_change)
        self.server_controller.set_error_callback(self.on_server_error)
        self.server_controller.set_server_callback(self.on_server_message)
        self.server_controller.set_servers_changed_callback(self.update_menu)
        
    def create_icon_image(self):
        """Create an icon for the system tray using the EliteA logo."""
        try:
            # Load the PNG logo using package resources (works when packaged)
            logo_data = self._load_icon_data()
            
            if logo_data:
                # Create image from binary data
                from io import BytesIO
                image = Image.open(BytesIO(logo_data))
                
                # Ensure the image is the right size
                if image.size != (64, 64):
                    image = image.resize((64, 64), Image.Resampling.LANCZOS)
                
                # Convert to RGBA if not already
                if image.mode != 'RGBA':
                    image = image.convert('RGBA')
                
                # For macOS, create a monochrome version for better tray integration
                if platform.system().lower() == "darwin":
                    # Create a monochrome version
                    # Convert to grayscale first
                    gray_image = image.convert('L')
                    
                    # Create a new RGBA image
                    mono_image = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
                    
                    # Apply the grayscale as an alpha mask to create white/transparent icon
                    for x in range(64):
                        for y in range(64):
                            # Get the grayscale value
                            gray_value = gray_image.getpixel((x, y))
                            
                            # Create white pixels where the logo is visible
                            if gray_value > 50:  # Threshold to avoid very faint pixels
                                # Use the grayscale value as alpha for smooth edges
                                alpha = min(255, gray_value + 50)  # Boost contrast slightly
                                mono_image.putpixel((x, y), (255, 255, 255, alpha))

                    self._draw_status_dot(mono_image)
                    return mono_image
                else:
                    # For other platforms, use the full-color logo
                    self._draw_status_dot(image)
                    return image
            else:
                # Fallback: create a simple icon if logo file not found
                icon = self._create_fallback_icon()
                self._draw_status_dot(icon)
                return icon
                
        except Exception as e:
            logger.warning(f"Could not load logo icon ({e}), using fallback")
            icon = self._create_fallback_icon()
            self._draw_status_dot(icon)
            return icon
    
    def _draw_status_dot(self, image):
        status = "config_error" if self.config_error else "running" if self.is_server_running else None
        if status:
            from PIL import ImageDraw
            draw = ImageDraw.Draw(image)
            dot_color = STATUS_DOT_COLORS[status]
            dot_radius = 12
            margin = 0
            x0 = image.width - dot_radius * 2 - margin
            y0 = image.height - dot_radius * 2 - margin
            x1 = image.width - margin
            y1 = image.height - margin
            draw.ellipse([x0, y0, x1, y1], fill=dot_color, outline=(255, 255, 255, 255), width=2)
    
    def _load_icon_data(self):
        """Load icon data using package resources (works when packaged)."""
        icon_file_name = 'logo.png' if (platform.system().lower() == "windows") else 'logo64x64.png'
        try:
            # Try modern importlib.resources first (Python 3.9+)
            try:
                import importlib.resources as resources
                
                # Try to load the icon from the package
                try:
                    # For Python 3.9+
                    icon_package = resources.files('elitea_mcp.icons')
                    icon_file = icon_package / icon_file_name
                    return icon_file.read_bytes()
                except AttributeError:
                    # For Python 3.9-3.10 compatibility
                    with resources.path('elitea_mcp.icons', icon_file_name) as icon_path:
                        return icon_path.read_bytes()
                        
            except (ImportError, ModuleNotFoundError):
                # Fallback to pkg_resources for older Python versions
                try:
                    import pkg_resources
                    return pkg_resources.resource_string('elitea_mcp', f'icons/{icon_file_name}')
                except (ImportError, FileNotFoundError):
                    pass
            
            # Final fallback: try direct file access (development mode)
            current_dir = os.path.dirname(os.path.abspath(__file__))
            logo_path = os.path.join(current_dir, 'icons', icon_file_name)
            
            if os.path.exists(logo_path):
                with open(logo_path, 'rb') as f:
                    return f.read()
                    
        except Exception as e:
            logger.error(f"Failed to load icon data: {e}")
            
        return None
    
    def _create_fallback_icon(self):
        """Create a fallback icon when the PNG logo is not available."""
        # Create a simple icon - a blue circle with "MCP" text
        width = 64
        height = 64
        image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        
        # Draw circle background
        draw.ellipse([2, 2, width-2, height-2], fill=(52, 152, 219, 255), outline=(41, 128, 185, 255), width=2)
        
        # Draw "MCP" text (simplified)
        try:
            # Try to use a font, fall back to default if not available
            from PIL import ImageFont
            try:
                font = ImageFont.truetype("Arial", 14)
            except OSError:
                font = ImageFont.load_default()
            draw.text((width//2, height//2), "MCP", fill=(255, 255, 255, 255), 
                     font=font, anchor="mm")
        except ImportError:
            # Fallback for basic drawing
            draw.text((18, 25), "MCP", fill=(255, 255, 255, 255))
            
        return image

    def get_menu_items(self):
        """Create the context menu for the tray icon, announcing a config error the moment it appears."""
        previous_error = str(self.config_error) if self.config_error else None
        config = self._read_config()
        if self.config_error and str(self.config_error) != previous_error:
            self.report_config_error()
        
        menu_items = [
            pystray.MenuItem("ELITEA MCP Client", lambda: None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        problem_count = len(self.config_report.notable) if config is not None else 0
        if problem_count:
            plural = "s" if problem_count > 1 else ""
            menu_items.append(pystray.MenuItem(f"Config has {problem_count} problem{plural} (Validate Config to see)",
                                               lambda: None, enabled=False))
        
        if config is None:
            menu_items.append(pystray.MenuItem("Config File Has Errors", lambda: None, enabled=False))
            if self.is_server_running:
                menu_items.append(pystray.MenuItem("Stop MCP Server", self.stop_server))
            menu_items.append(pystray.Menu.SEPARATOR)
        elif config.get('deployment_url') and config.get('auth_token'):
            if not self.is_server_running:
                # Show start options when server is not running
                menu_items.extend([
                    pystray.MenuItem("Start MCP Server", lambda: self.start_server(daemon_mode=False))
                ]),
            else:
                menu_items.extend([
                    pystray.MenuItem(f"Stop MCP Server", self.stop_server),
                    pystray.MenuItem("Restart MCP Server", self.restart_server),
                ])
            menu_items.append(pystray.Menu.SEPARATOR)

        servers_menu = self.build_servers_menu()
        if servers_menu:
            menu_items.extend([pystray.MenuItem("Servers", servers_menu), pystray.Menu.SEPARATOR])
        
        menu_items.extend([
            pystray.MenuItem("Configuration", pystray.Menu(
                pystray.MenuItem("Open Config File", self.open_config_file),
                pystray.MenuItem("Validate Config", self.validate_config),
                pystray.MenuItem("Open Config Folder", self.open_config_folder),
                pystray.MenuItem("Bootstrap (Terminal)", self.bootstrap_config),
                pystray.MenuItem("View Current Config", self.view_config),
            )),
            pystray.Menu.SEPARATOR
        ])
        
        if self.update_checker:
            update_state = self.update_checker.state
            menu_items.append(pystray.MenuItem("Check for Updates", self.check_for_updates))
            if update_state.can_update:
                menu_items.append(
                    pystray.MenuItem(f"Update to {update_state.latest} and Restart", self.update_and_restart))
            menu_items.append(pystray.Menu.SEPARATOR)
        
        diagnostics_label = "Running Diagnostics…" if self.diagnostics_running else "Run Diagnostics"
        menu_items.extend([
            pystray.MenuItem("Help", pystray.Menu(
                pystray.MenuItem(diagnostics_label, self.run_diagnostics, enabled=not self.diagnostics_running),
            )),
            pystray.MenuItem("About", self.show_about),
            pystray.MenuItem("Quit", self.quit_app),
        ])
        
        return menu_items

    def build_servers_menu(self):
        """One row per server of the current or last run; a failed or declined row opens its error details."""
        entries = list(self.server_controller.list_servers())
        if not entries:
            return None
        rows = [
            pystray.MenuItem(entry.format_menu_row(), functools.partial(self.open_server_error, entry.name))
            if entry.status == FAILED or entry.registration == DECLINED
            else pystray.MenuItem(entry.format_menu_row(), lambda: None)
            for entry in entries
        ]
        has_failed = any(entry.status == FAILED for entry in entries)
        return pystray.Menu(*rows, pystray.Menu.SEPARATOR,
                            pystray.MenuItem("Retry Failed Servers", self.retry_failed_servers, enabled=has_failed))

    def retry_failed_servers(self, icon=None, item=None):
        self.server_controller.retry_failed_servers()

    def run_diagnostics(self, icon=None, item=None):
        """Run every check off the menu thread, which may take as long as the slowest server's start."""
        if self.diagnostics_running:
            return
        self.diagnostics_running = True
        self.update_menu()
        self.show_notification("Diagnostics", "Running diagnostics…")
        threading.Thread(target=self.write_diagnostics_report, name="diagnostics", daemon=True).start()

    def write_diagnostics_report(self):
        """Save the report next to the logs, open it and copy it, since a notification cannot hold it."""
        error_dir = get_default_paths()["tray_log"].parent
        try:
            results = run_checks(DoctorContext(get_config_file(), error_dir, self.list_local_session))
            report = format_report(results)
            path = error_dir / REPORT_FILE_NAME
            path.write_text(report, encoding="utf-8")
            copied = self._copy_to_clipboard(report)
            self._open_file(path)
            delivery = "Report opened and copied to the clipboard." if copied else f"Report opened: {path}"
            self.show_notification("Diagnostics", f"{summarize(results)} {delivery}")
        except Exception as error:
            logger.exception("Diagnostics failed")
            self.show_error("Diagnostics", f"Could not run diagnostics: {error}")
        finally:
            self.diagnostics_running = False
            self.update_menu()

    def list_local_session(self):
        """This tray's own running servers; a daemon-mode session runs in another process, which publishes its own status."""
        controller = self.server_controller
        return controller.list_servers() if controller.is_running and not controller.daemon_mode else None

    def open_server_error(self, name, icon=None, item=None):
        """Write the server's reason, hint and last output to <server>-error.txt and open it."""
        report = self.server_controller.format_server_error(name)
        if report is None:
            return
        path = get_default_paths()["tray_log"].parent / f"{name}-error.txt"
        try:
            path.write_text(report, encoding="utf-8")
        except OSError as error:
            self.show_error("Server Details", f"Could not write {path}: {error}")
            return
        self._open_file(path)

    def on_server_message(self, message):
        self.update_menu()
        self.show_notification("MCP Servers", message)

    def _read_config(self):
        """Return the bootstrap config, or None with the error kept for the menu when the file cannot be parsed or a problem stops the client."""
        try:
            raw = load_config()
        except ConfigError as error:
            self.config_error, self.config_report, self.settings = error, None, None
            return None
        self.config_report = check_config(raw)
        self.settings = get_bootstrap_config(raw)
        self.config_error = self.config_report if self.config_report.config is None else None
        return None if self.config_error else self.settings

    def report_config_error(self):
        """Log the full error with its source excerpt and notify its short form, which fits a notification."""
        logger.error(str(self.config_error))
        self.show_notification("Invalid Config", self.config_error.summary)

    def validate_config(self, icon=None, item=None):
        """Re-read the config file and report the result, bringing the server controls back once it is fixed."""
        config = self._read_config()
        self.update_menu()
        write_config_schema()
        if config is None:
            self.report_config_error()
        elif self.config_report.notable:
            logger.warning(str(self.config_report))
            self.show_notification("Config Has Problems", self.config_report.summary)
        else:
            self.show_notification("Config Valid", f"{get_config_file()} is valid.")

    def on_update_state_change(self, state):
        """Refresh the menu after a background check and announce a newly found release."""
        self.update_menu()
        if state.can_update:
            self.show_notification("Update Available", state.describe())

    def check_for_updates(self, icon=None, item=None):
        """Check PyPI on a worker thread so the menu stays responsive, then report the outcome."""
        def check():
            self.update_checker.refresh()
            self.update_menu()
            self.show_notification("Check for Updates", self.update_checker.state.describe())

        threading.Thread(target=check, name="manual-update-check", daemon=True).start()

    def update_and_restart(self, icon=None, item=None):
        """Stop the servers and hand the upgrade to a detached helper, or show the command to run by hand."""
        state = self.update_checker.state
        plan = resolve_upgrade_plan()
        if not plan.can_run:
            clipboard_note = " (copied to clipboard)" if self._copy_to_clipboard(plan.display_command) else ""
            self.show_notification("Update Manually", f"{plan.reason} Run: {plan.display_command}{clipboard_note}")
            return
        servers_were_running = self.is_server_running
        server_daemon_mode = self.server_controller.daemon_mode
        self.show_notification("Updating", f"Updating to {state.latest}. The tray will restart.")
        self.stop_server()
        try:
            start_update_helper(plan, state.current, state.latest, servers_were_running, server_daemon_mode,
                                get_default_paths())
        except OSError as error:
            self.show_error("Update Failed", f"Could not start the updater: {error}")
            if servers_were_running:
                self.start_server(daemon_mode=server_daemon_mode)
            return
        self.update_checker.stop()
        if self.icon:
            self.icon.stop()

    def report_update_result(self):
        """Tell the user how the last update went and bring back the servers it stopped."""
        result = consume_update_result(get_default_paths()["update_result"])
        if not result:
            return
        if result["error"]:
            self.show_error("Update Failed", f"{result['error']}. See log: {result['log']}")
        else:
            self.show_notification("Update Complete", f"Updated to {result['installed']}.")
        if result["servers_were_running"]:
            self.start_server(daemon_mode=result["server_daemon_mode"])

    def _copy_to_clipboard(self, text):
        """Put text on the clipboard with the platform's own tool, reporting whether it worked."""
        for command in CLIPBOARD_COMMANDS.get(platform.system().lower(), []):
            try:
                subprocess.run(command, input=text, text=True, check=True, timeout=5)
                return True
            except (OSError, subprocess.SubprocessError, UnicodeError):
                continue
        return False

    def on_server_status_change(self, status: str):
        """Handle server status changes."""
        self.is_server_running = self.server_controller.is_running
        self.update_menu()
        if status:
            self.show_notification("Server Status", status)
    
    def on_server_error(self, error: str):
        """Handle server errors."""
        self.is_server_running = False
        self.update_menu()
        self.show_error("Server Error", f"{error} {TRAY_DIAGNOSTICS_HINT}")

    def start_server(self, daemon_mode: bool = False):
        """Start the MCP server using the server controller.
        
        Args:
            daemon_mode: If True, start as separate daemon process. If False, start in current process.
        """
        if self.is_server_running:
            return
            
        config = self._read_config()
        if config is None:
            self.report_config_error()
            self.update_menu()
            return
        if not config.get('deployment_url') or not config.get('auth_token'):
            self.show_error("Configuration Required", 
                          "Please configure the client first using Bootstrap.")
            return
            
        # Use the server controller to start servers
        success = self.server_controller.start_servers(daemon_mode=daemon_mode)
        if not success:
            mode_str = "daemon" if daemon_mode else "embedded"
            self.show_error("Server Start Failed", f"Failed to start MCP servers in {mode_str} mode. {TRAY_DIAGNOSTICS_HINT}")

    def stop_server(self):
        """Stop the MCP server using the server controller."""
        if not self.is_server_running:
            return

        self.server_controller.stop_servers()

    def restart_server(self):
        """Restart the MCP server to reload configuration."""
        if self._read_config() is None:
            self.report_config_error()
            self.update_menu()
            return
        self.server_controller.restart_servers()

    def open_config_file(self, icon=None, item=None):
        """Open the configuration file in the default text editor."""
        config_file = get_config_file()
        
        # Ensure the config file exists
        if not config_file.exists():
            # Create an empty config file
            config_file.parent.mkdir(parents=True, exist_ok=True)
            config_file.write_text(json.dumps({"$schema": SCHEMA_REFERENCE}, indent=2))
            write_config_schema()
            logger.info(f"Created config file: {config_file}")
        
        self._open_file(config_file)
        logger.info(f"Opening config file: {config_file}")

    def open_config_folder(self, icon=None, item=None):
        """Open the configuration folder in the file manager."""
        config_file = get_config_file()
        config_folder = config_file.parent
        
        # Ensure the config folder exists
        config_folder.mkdir(parents=True, exist_ok=True)
        
        self._open_file(config_folder)
        logger.info(f"Opening config folder: {config_folder}")

    def _open_file(self, file_path):
        """Open a file or folder using the system's default application."""
        try:
            system = platform.system().lower()
            
            if system == "darwin":  # macOS
                # Create environment for subprocess to handle fork safety
                env = os.environ.copy()
                env['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
                
                subprocess.run(
                    ["open", str(file_path)], 
                    check=True,
                    env=env,
                    start_new_session=True
                )
            elif system == "linux":
                subprocess.run(["xdg-open", str(file_path)], check=True)
            elif system == "windows":
                os.startfile(str(file_path))
            else:
                logger.warning(f"Unsupported platform: {system}")
                logger.info(f"Please manually open: {file_path}")
        except Exception as e:
            logger.error(f"Failed to open {file_path}: {e}")
            logger.info(f"Please manually open: {file_path}")

    def bootstrap_config(self, icon=None, item=None):
        """Run the interactive bootstrap configuration in terminal."""
        logger.info("\n" + "="*50)
        logger.info("Running Bootstrap Configuration...")
        logger.info("="*50)
        
        try:
            # Run bootstrap in the current process
            config = interactive_bootstrap()
            logger.info("Configuration updated successfully!")
            self.update_menu()
            self.show_notification("Bootstrap Complete", "Configuration has been updated.")
        except Exception as e:
            error_msg = f"Bootstrap failed: {str(e)}"
            logger.error(error_msg)
            self.show_error("Bootstrap Error", error_msg)

    def view_config(self, icon=None, item=None):
        """Display current configuration via notification."""
        config = self._read_config()
        if config is None:
            self.report_config_error()
            self.update_menu()
            return
        try:
            # Build configuration summary for notification
            config_lines = []
            
            # Basic configuration
            config_lines.append(f"Deployment URL: {config.get('deployment_url', 'Not set')}")
            
            # Auth token (masked)
            auth_token = config.get('auth_token', '')
            if auth_token:
                masked_token = '*' * 8 + auth_token[-4:] if len(auth_token) > 4 else '*' * 8
                config_lines.append(f"Auth Token: {masked_token}")
            else:
                config_lines.append("Auth Token: Not set")
            
            config_lines.append(f"Host: {config.get('host', '0.0.0.0')}")
            config_lines.append(f"Port: {config.get('port', 8000)}")
            
            project_id = config.get('project_id', 'Not set')
            config_lines.append(f"Project ID: {project_id}")
            
            # Servers summary
            servers = config.get('servers', {})
            if servers:
                config_lines.append(f"Servers: {len(servers)} configured")
                # Show first few server names if any
                server_names = list(servers.keys())[:3]  # Show max 3 servers
                for server_name in server_names:
                    config_lines.append(f"  • {server_name}")
                if len(servers) > 3:
                    config_lines.append(f"  • ... and {len(servers) - 3} more")
            else:
                config_lines.append("Servers: None configured")
            
            # Create notification message
            message = "\n".join(config_lines)
            
            # Show notification
            self.show_notification("Current Configuration", message)
            
            # Also log for debugging (keep original behavior)
            logger.info("\n" + "="*50)
            logger.info("Current Configuration:")
            logger.info("="*50)
            for line in config_lines:
                logger.info(line)
            logger.info(f"\nConfig file location: {get_config_file()}")
            logger.info("="*50)
            
        except Exception as e:
            logger.error(f"Error viewing configuration: {e}")
            self.show_error("Configuration Error", f"Failed to load configuration: {str(e)}")

    def show_about(self, icon=None, item=None):
        """Show about information via notification."""
        try:
            version = get_installed_version() or "unknown (development checkout)"
            
            # Create about message
            title = "ELITEA MCP Client"
            message = f"Version: {version}\n\nModel Context Protocol client for the ELITEA platform.\n\nProvides MCP server management and configuration."
            
            # Show notification
            self.show_notification(title, message)
            
            # Also log for debugging
            logger.info(f"About dialog shown - {title} v{version}")
            
        except Exception as e:
            logger.error(f"Error showing about dialog: {e}")
            # Fallback notification
            self.show_notification("ELITEA MCP Client", "MCP client for ELITEA platform")

    def show_error(self, title, message):
        """Log the whole error; the notification is cut to fit, because Windows refuses balloon text over 256 characters."""
        text = f"{title}: {message}"
        logger.error(text)
        if self.icon:
            self.icon.notify(text if len(text) <= NOTIFICATION_LIMIT else text[:NOTIFICATION_LIMIT - 3] + "...", "Error")

    def show_notification(self, title, message):
        """Show notification using the system tray."""
        logger.info(f"{title}: {message}")
        
        # Use platform-specific notification system
        if platform.system().lower() == "darwin":
            self._show_macos_notification(title, message)
        else:
            # For other platforms, use pystray's built-in notification
            if self.icon:
                self.icon.notify(message, title)
    
    def _show_macos_notification(self, title, message):
        """Show native macOS notification using osascript."""
        try:
            # Escape quotes and special characters for AppleScript
            safe_title = title.replace('\\', '\\\\').replace('"', '\\"')
            safe_message = message.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
            
            # Use osascript to show native macOS notification
            applescript = f'''
            display notification "{safe_message}" with title "{safe_title}" sound name "default"
            '''
            
            # Create environment for subprocess to handle fork safety
            env = os.environ.copy()
            env['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
            
            subprocess.run(
                ["osascript", "-e", applescript],
                check=True,
                env=env,
                capture_output=True,
                text=True,
                start_new_session=True
            )
            
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to show macOS notification via osascript: {e}")
            # Fallback to pystray notification
            if self.icon:
                # For fallback, use single line message to avoid Script Editor issue
                single_line_message = message.replace('\n', ' | ')
                self.icon.notify(single_line_message, title)
        except Exception as e:
            logger.warning(f"Error showing macOS notification: {e}")
            # Fallback to pystray notification
            if self.icon:
                single_line_message = message.replace('\n', ' | ')
                self.icon.notify(single_line_message, title)

    def update_menu(self):
        """Update the tray menu."""
        if self.icon:
            self.icon.menu = pystray.Menu(*self.get_menu_items())
            self.icon.icon = self.create_icon_image()

    def quit_app(self, icon=None, item=None):
        """Quit the application."""
        if self.is_server_running:
            self.stop_server()
        if self.update_checker:
            self.update_checker.stop()
        if self.icon:
            self.icon.stop()

    def _on_tray_ready(self, icon):
        """Show the icon, report a finished update and start the background update checks."""
        icon.visible = True
        if self.config_error:
            self.report_config_error()
        self.report_update_result()
        if self.update_checker:
            self.update_checker.start()

    def run(self):
        """Start the tray application."""
        # Hide from dock on macOS
        self._hide_from_dock()
        
        image = self.create_icon_image()
        menu = pystray.Menu(*self.get_menu_items())
        
        self.icon = pystray.Icon(
            "elitea_mcp",
            image,
            "Elitea MCP Client",
            menu
        )
        
        logger.info("Starting EliteA MCP Client tray application...")
        logger.info("Right-click the tray icon for options")
        logger.info("Use 'Open Config File' to edit configuration")
        self.icon.run(setup=self._on_tray_ready)

    def _hide_from_dock(self):
        """Hide the application from the macOS dock."""
        if platform.system().lower() == "darwin":
            try:
                # Import PyObjC modules for macOS
                from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
                
                # Get the shared application instance
                app = NSApplication.sharedApplication()
                
                # Set activation policy to accessory (no dock icon)
                app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
                
                logger.info("Successfully hidden from macOS dock")
                
            except ImportError:
                logger.warning("PyObjC not available, cannot hide from dock")
                logger.info("Install with: pip install pyobjc-framework-Cocoa")
            except Exception as e:
                logger.warning(f"Failed to hide from dock: {e}")
        else:
            # Not macOS, no action needed
            pass


def run_tray():
    """Entry point for the tray application."""
    # Set fork safety for macOS BEFORE any other imports or operations
    if platform.system() == "Darwin":
        os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    
    # Set up file logging
    try:
        default_paths = get_default_paths()
        log_file = default_paths['tray_log']
        setup_file_logger(str(log_file))
        logger.info(f"Logging to file: {log_file}")
    except Exception as e:
        logger.warning(f"Could not set up file logging: {e}")
    
    app = MCPTrayApp()
    app.run()


def main():
    """Main entry point that sets up environment before any imports."""
    import platform
    import os
    
    # Critical: Set fork safety for macOS BEFORE importing pystray or any GUI frameworks
    if platform.system() == "Darwin":
        os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
        
        # Also set multiprocessing method to spawn
        try:
            import multiprocessing
            multiprocessing.set_start_method('spawn', force=True)
        except RuntimeError:
            pass  # Already set
    
    # Now it's safe to run the tray
    run_tray()


if __name__ == "__main__":
    main()
