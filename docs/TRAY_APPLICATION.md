# Tray Application

The EliteA MCP Client includes a system tray application that provides easy access to configuration and server management th    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/elitea-mcp</string>
        <string>tray</string>
        <string>--daemon</string>
    </array>a graphical interface.

## Features

- **System Tray Icon**: Sits in your system tray for quick access
- **Configuration Management**: Open config files directly, run terminal-based bootstrap, and view configuration
- **Server Control**: Start and stop MCP servers with a single click
- **Real-time Status**: Visual feedback for server status changes
- **Cross-platform**: Works on Windows, macOS, and Linux (no GUI dependencies required)

## Usage

### Starting the Tray Application

You can start the tray application in several ways:

1. **Using the CLI command (recommended):**
   ```bash
   elitea-mcp tray
   ```

2. **Using daemon mode:**
   ```bash
   elitea-mcp tray --daemon
   ```

3. **Running directly via Python module:**
   ```bash
   python -m elitea_mcp.tray
   ```

### Tray Menu Options

Right-click on the tray icon to access:

- **Server Control** (when configured and not running):
  - **Start MCP Server (Embedded)**: Start servers in the same process as the tray app
  - **Start MCP Server (Daemon)**: Start servers as a separate background daemon process
- **Stop MCP Server**: Stop running servers (shows current mode: Embedded/Daemon)
- **Restart MCP Server**: Reload config by stopping and starting servers
- **Configuration**:
  - **Open Config File**: Open the JSON configuration file in your default text editor
  - **Open Config Folder**: Open the configuration directory in your file manager
  - **Bootstrap (Terminal)**: Run the interactive bootstrap configuration in the terminal
  - **View Current Config**: Display current configuration in the console
- **About**: Show application information in the console
- **Quit**: Exit the tray application

### Configuration Management

The tray application provides file-based configuration management:

- **Open Config File**: Opens the JSON configuration file (`~/.config/elitea-mcp/config.json` on Linux/macOS, `%APPDATA%\elitea-mcp\config.json` on Windows) in your default text editor
- **Open Config Folder**: Opens the configuration directory in your system's file manager
- **Bootstrap (Terminal)**: Runs the interactive bootstrap configuration in the current terminal
- **View Current Config**: Displays the current configuration in the console with sensitive data masked

This approach provides:
- **No GUI dependencies**: Works in environments without tkinter or other GUI libraries
- **Direct file editing**: Use your preferred text editor for configuration
- **Version control friendly**: JSON files can be easily versioned and shared
- **Cross-platform compatibility**: Uses system defaults for file opening
- **Host**: Server host (default: 0.0.0.0)
- **Port**: Server port (default: 8000)

### Server Management

When properly configured, the tray application allows you to:

- **Start MCP servers** in two modes:
  - **Embedded Mode**: Servers run in the same process as the tray application
  - **Daemon Mode**: Servers run as a separate background daemon process (`elitea-mcp serve --daemon`)
- **Stop running servers** gracefully (both embedded and daemon modes)
- **View server status** through notifications with mode indicators
- **Monitor configured servers** with real-time status updates

#### Server Modes Comparison

**Embedded Mode:**
- Servers run within the tray application process
- Lower resource usage
- Servers stop when tray application is closed
- Immediate status feedback

**Daemon Mode:**
- Servers run as independent background process
- Higher isolation and stability  
- Servers continue running even if tray is closed
- Can be managed independently via `elitea-mcp serve` commands

## System Requirements

- Python 3.10+
- Required dependencies:
  - `pystray>=0.19.0`
  - `pillow>=8.0.0`
  - `tkinter` (usually included with Python)

### Daemon Mode

Both the tray application and server can be run in daemon mode (background process):

```bash
# Run tray in daemon mode
elitea-mcp tray --daemon

# Run server in daemon mode  
elitea-mcp serve --daemon

# Specify custom PID and log files
elitea-mcp tray --daemon --pid-file /path/to/tray.pid --log-file /path/to/tray.log
```

### Platform-specific Requirements

- **macOS**: Requires `pyobjc-framework-Quartz`
- **Linux**: May require additional GUI libraries
- **Windows**: No additional requirements

## Daemon Mode

Both the MCP server and tray application support daemon mode for background operation:

```bash
# Run MCP server as daemon
elitea-mcp serve --daemon

# Run tray application as daemon (background mode)
elitea-mcp tray --daemon
```

Daemon mode options:
- `--daemon`: Run in background (Unix systems only)
- `--pid-file`: Specify PID file location (default: platform-specific)
- `--log-file`: Specify log file location (default: platform-specific)
- Log files automatically rotate when they reach **5 MB**, keeping up to **5** backups

## Automatic Startup

### Service Configuration Files

The project includes service configuration files for automatic startup:

- **macOS**: `scripts/com.elitea.mcp.tray.plist` (LaunchAgent)
- **Linux**: `scripts/elitea-mcp-tray@.service` (systemd user service)
- **Windows**: `scripts/elitea-mcp-tray.xml` (Task Scheduler)

These can be manually installed to set up automatic startup.

### Manual Setup

#### macOS (LaunchAgent)

Create a launch agent file at `~/Library/LaunchAgents/com.elitea.mcp.tray.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.elitea.mcp.tray</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/elitea-mcp</string>
        <string>tray</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

Then load it:
```bash
launchctl load ~/Library/LaunchAgents/com.elitea.mcp.tray.plist
```

### Linux (systemd user service)

Create a user service file at `~/.config/systemd/user/elitea-mcp-tray.service`:

```ini
[Unit]
Description=EliteA MCP Tray Application
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/local/bin/elitea-mcp tray --daemon
Restart=always
RestartSec=10
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
```

Enable and start:
```bash
systemctl --user enable elitea-mcp-tray.service
systemctl --user start elitea-mcp-tray.service
```

### Windows (Task Scheduler)

1. Open Task Scheduler
2. Create Basic Task
3. Set trigger to "When I log on"
4. Set action to start `elitea-mcp tray` command

## Troubleshooting

### Tray Icon Not Appearing

- Ensure your system supports system tray icons
- Check if tray icons are enabled in your desktop environment
- On some Linux systems, you may need to install a tray extension

### Configuration Issues

- Use "Bootstrap" from the tray menu to reconfigure
- Check that config files have proper permissions
- Verify network connectivity to your deployment URL

### Server Start Failures

- Ensure servers are properly configured via bootstrap
- Check that all required MCP servers are available
- Verify authentication credentials are valid

### GUI Issues

- Ensure tkinter is available (`python -m tkinter`)
- Check that X11 forwarding is enabled on remote systems
- Verify display environment variables are set correctly

## Development

The tray application is built using:

- **pystray**: For system tray integration
- **tkinter**: For configuration dialogs
- **PIL/Pillow**: For icon generation
- **threading**: For non-blocking operations

Key components:

- `MCPTrayApp`: Main tray application class
- `ServerController`: Manages MCP server lifecycle
- Configuration dialogs for user interaction
- Icon generation for cross-platform compatibility
