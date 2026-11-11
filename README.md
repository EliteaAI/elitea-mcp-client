# EliteA MCP Client

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![PyPI version](https://badge.fury.io/py/elitea-mcp.svg)](https://badge.fury.io/py/elitea-mcp)

A powerful and user-friendly MCP (Model Context Protocol) client with both command-line interface and system tray application. Provides seamless integration with the ELITEA platform for managing MCP servers and AI agents.

## ✨ Features

- 🖥️ **System Tray Application** - Easy-to-use GUI with cross-platform support
- 🚀 **Command Line Interface** - Full-featured CLI for developers and automation
- 🔧 **Interactive Bootstrap** - Step-by-step configuration wizard
- 🌐 **Multiple Transports** - Support for stdio and SSE (Server-Sent Events)
- 🔄 **Stateful & Stateless Sessions** - Flexible server connection modes
- 🐧 **Cross-Platform** - Works on Windows, macOS, and Linux
- 📦 **Easy Installation** - Available via PyPI with pipx support
- 🛡️ **Production Ready** - Comprehensive error handling and logging

### Feature Comparison

| Feature | CLI Mode | Tray Mode | Daemon Mode |
|---------|----------|-----------|-------------|
| Server Management | ✅ Manual | ✅ GUI Controls | ✅ Background |
| Configuration | ✅ Bootstrap CMD | ✅ File/Wizard | ✅ File-based |
| User Interface | 💻 Terminal | 🖥️ System Tray | 🔄 Background |
| Auto-start | ❌ Manual | ✅ OS Integration | ✅ Service Mode |
| Resource Usage | 💾 On-demand | 💾 Minimal | 💾 Continuous |
| Best For | Development | Daily Use | Production |

## 🚀 Quick Start

### Installation

#### Using pipx (Recommended)

```bash
# Install pipx if not already installed
pip install pipx

# Install elitea-mcp
pipx install elitea-mcp
```

#### Using pip

```bash
pip install elitea-mcp
```

### Configuration

```bash
# Interactive configuration
elitea-mcp bootstrap

# Or with command line parameters
elitea-mcp bootstrap --deployment_url https://api.example.com --auth_token YOUR_TOKEN
```

### Start System Tray Application

```bash
# Launch the system tray application
elitea-mcp tray

# Or run in background (daemon mode)
elitea-mcp tray --daemon
```

### Example Usage

```bash
# 1. Configure the client
elitea-mcp bootstrap --deployment_url https://api.elitea.com --auth_token your_token_here

# 2. Run with a specific project
elitea-mcp run --project_id 123

# 3. Or start the tray app for GUI management
elitea-mcp tray
```

## 📋 Table of Contents

- [Installation](#installation)
- [System Tray Application](#system-tray-application)
- [Command Line Usage](#command-line-usage)
- [Configuration](#configuration)
- [Server Management](#server-management)
- [Platform-Specific Notes](#platform-specific-notes)
- [Development](#development)
- [Documentation](#documentation)
- [Contributing](#contributing)

## 💻 Installation

### Prerequisites

- Python 3.11 or higher
- pip or pipx package manager

### Method 1: Using pipx (Recommended)

[pipx](https://pypa.github.io/pipx/) installs the package in an isolated environment while making CLI commands globally available.

#### macOS

```bash
# Install pipx
brew install pipx
pipx ensurepath

# Install elitea-mcp
pipx install elitea-mcp

# Add to shell profile (if needed)
echo 'export PATH="$PATH:$HOME/.local/bin"' >> ~/.zprofile
source ~/.zprofile
```

#### Linux

```bash
# Install pipx
sudo apt install pipx  # Ubuntu/Debian
# or
sudo dnf install pipx  # Fedora
# or
pip install --user pipx

# Ensure PATH
pipx ensurepath

# Install elitea-mcp
pipx install elitea-mcp
```

#### Windows

```powershell
# Install pipx
pip install --user pipx
python -m pipx ensurepath

# Install elitea-mcp
pipx install elitea-mcp
```

### Method 2: Using pip

```bash
pip install elitea-mcp
```

### Method 3: From Source

```bash
git clone https://github.com/EliteaAI/elitea-mcp-client.git
cd elitea-mcp-client
pip install -e .
```

## 🎮 System Tray Application

The system tray application provides an intuitive GUI for managing your MCP client without requiring terminal knowledge.

### Features

- **🖥️ System Tray Integration** - Minimalist icon in your system tray
- **📁 Configuration Management** - Open config files, run bootstrap wizard
- **🔄 Server Control** - Start/stop MCP servers with different modes
- **📊 Real-time Status** - Visual feedback for server operations
- **🌍 Cross-Platform** - Windows, macOS, and Linux support
- **🚫 No GUI Dependencies** - Works in all environments

### Starting the Tray App

```bash
# Launch tray application
elitea-mcp tray

# Run in background (daemon mode)
elitea-mcp tray --daemon

# Using launch scripts (platform-specific)
./scripts/launch-tray-macos.sh      # macOS
./scripts/launch-tray-windows.bat   # Windows
```

### Tray Menu Options

Right-click the tray icon to access:

#### Server Control
- **Start MCP Server (Embedded)** - Run servers within the tray process
- **Start MCP Server (Daemon)** - Run servers as background daemon
- **Stop MCP Server** - Stop running servers (shows current mode)
- **Restart MCP Server** - Reload configuration by stopping and starting servers

#### Help
- **Run Diagnostics** - Run every `elitea-mcp doctor` check, open the report and copy it to the clipboard

#### Servers
Shown after the first start, with one row per configured server:
- `✓ jira — 14 tools, registered` - the server runs and ELITEA has its tools
- `⚠ github — declined (name already registered)` - another running client, or a session that has not timed out yet, holds this name in the project
- `✗ confluence — failed: command "uvx" not found` - the server could not start; the other servers are not affected

Click a failed or declined row to open `<server>-error.txt` (next to the tray log) with the reason, a hint and the server's last output, with configured secrets (`auth_token`, `env` and `headers` values) replaced by `****`. In these messages a server URL keeps its host and path, its user name and password are removed, and every query and fragment value is shown as `****`. A token written into the URL path (for example `https://mcp.example.com/s/<token>/mcp`) stays visible on this machine; ELITEA only ever receives the URL's scheme and host.
**Retry Failed Servers** loads only the failed servers again; healthy ones keep running. A server that timed out, could not be reached, or broke after it was working is also retried automatically after about 30 s, 1 min, 2 min and 5 min, then every 5 min for as long as the problem lasts; its row shows `(retrying automatically)` while a retry is pending. When every server fails at start the client does not connect and does not retry on its own; use **Retry Failed Servers**. Each wait is randomised (between half and all of the step) so clients that lost the same server do not all retry at once, and a server that answers 429 or 503 with `Retry-After` is not tried again sooner than it asked. A server that fails again within 10 min of recovering keeps the longer wait. A missing command, an invalid config or a rejected token is never retried automatically.

After a start, a notification summarises the result, for example `3 of 4 MCP servers connected. Failed: confluence (command "uvx" not found).` When every server fails, the client does not connect to ELITEA.

#### Configuration
- **Open Config File** - Edit JSON configuration in default text editor
- **Validate Config** - Re-check the config file: a JSON error is reported with its line, column and fix hint, and every other problem with its path (for example `servers.github.args`) and what to change. While a problem stops the client the tray stays up with only the config tools, then Start returns once it validates. A server whose config cannot work is skipped at Start and the others run
- **Open Config Folder** - Open configuration directory in file manager
- **Bootstrap (Terminal)** - Run interactive configuration setup
- **View Current Config** - Display current settings via system notification

#### Updates
- **Check for Updates** - Check PyPI now and report whether you are up to date, an update is available, or the check failed
- **Update to x.y.z and Restart** - Shown when an update is available; upgrades with the tool you installed with (pipx, uv, or pip), restarts the tray on the new version, and starts the servers again if they were running

The tray checks for updates at startup and then every 24 hours. Set `"update_check": false` in `config.json` to turn checks off, or `"update_check_interval_hours"` to change the interval. If the tray cannot run the upgrade itself (for example, a development install or no write permission), it shows the command to run and copies it to the clipboard. The upgrade output goes to `elitea-mcp-update.log` next to the tray log.

#### Application
- **About** - Application information and version
- **Quit** - Exit the tray application

### Auto-Start Setup

#### macOS
```bash
# Copy launch script to Applications
cp scripts/launch-tray-macos.sh ~/Applications/

# Add to Login Items in System Preferences
# or create a Launch Agent (see docs/TRAY_SETUP_GUIDE.md)
```

#### Linux
```bash
# Add to autostart
mkdir -p ~/.config/autostart
cp scripts/elitea-mcp-tray.desktop ~/.config/autostart/
```

#### Windows
```powershell
# Copy to Startup folder
# Press Win+R, type shell:startup, then copy launch-tray-windows.bat
```

## 💻 Command Line Usage

### Available Commands

```bash
elitea-mcp --help                    # Show all available commands
elitea-mcp bootstrap                 # Configure deployment URL and auth token
elitea-mcp run                       # Run MCP client with specific project/app
elitea-mcp serve                     # Start MCP servers in background
elitea-mcp tray                      # Launch system tray application
elitea-mcp validate                  # List every problem in the config file; exit 1 when any is an error, 0 otherwise; --config <path> checks another file
elitea-mcp doctor                    # Check the whole local setup, one line per check; exit 1 when any check fails; --config <path> checks another file
elitea-mcp --version                 # Print the installed version
```

### Running MCP Client

#### With Specific Application

```bash
# Run with specific project and application
elitea-mcp run --project_id YOUR_PROJECT_ID --app_id YOUR_APP_ID

# Include version (optional)
elitea-mcp run --project_id YOUR_PROJECT_ID --app_id YOUR_APP_ID --version_id YOUR_VERSION_ID
```

#### With All Project Agents

```bash
# Use all available agents in a project
elitea-mcp run --project_id YOUR_PROJECT_ID
```

#### Transport Options

```bash
# Default: stdio transport
elitea-mcp run --project_id YOUR_PROJECT_ID

# SSE transport for web applications
elitea-mcp run --project_id YOUR_PROJECT_ID --transport sse --port 8000
```

### Server Management

```bash
# Start servers in background
elitea-mcp serve --daemon

# Custom PID and log files
elitea-mcp serve --daemon --pid-file /path/to/app.pid --log-file /path/to/app.log

# View running servers
ps aux | grep elitea-mcp
```

## ⚙️ Configuration

### Configuration Locations

The client stores configuration in your OS's standard app data directory:

- **Windows**: `%APPDATA%\elitea-mcp-client`
- **macOS**: `~/Library/Application Support/elitea-mcp-client`
- **Linux**: `~/.config/elitea-mcp-client`

Next to `config.json` the client keeps `client_id`, which tells ELITEA that a reconnecting or restarted client is the same installation. It is tied to the machine: a copy on another machine is replaced by a new id. A second copy of the client running at the same time (for example the tray and `elitea-mcp serve`) uses a temporary id, so its servers with the same names are declined instead of taking over the first copy's.

### Bootstrap Configuration

#### Interactive Mode

```bash
elitea-mcp bootstrap
```

This launches an interactive wizard that guides you through:
1. **Deployment URL** - Your ELITEA platform endpoint
2. **Authentication Token** - Your API authentication token
3. **Server Configuration** - Host and port settings
4. **MCP Server Setup** - Configure available MCP servers
   - When prompted for **Args**, enter all arguments in a single line separated
     by spaces. They will be stored as a list in the configuration file.

#### Command Line Mode

```bash
elitea-mcp bootstrap \
  --deployment_url https://api.example.com \
  --auth_token YOUR_TOKEN \
  --host 0.0.0.0 \
  --port 8000
```

### Configuration File Structure

```json
{
  "$schema": "./config.schema.json",
  "deployment_url": "https://api.example.com",
  "auth_token": "your-auth-token",
  "project_id": 123,
  "host": "0.0.0.0",
  "port": 8000,
  "timeout": 30,
  "startup_timeout": 30,
  "servers": {
    "server1": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "env": {},
      "stateful": false,
      "startup_timeout": 120
    },
    "remote_server": {
      "type": "http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer your-token"}
    }
  }
}
```

- A server without `type` is a local `stdio` server. A remote server needs `"type": "http"` (Streamable HTTP) or `"type": "sse"`; `headers` is optional.
- Snippets written for other MCP clients often use `mcpServers` instead of `servers`; the client reads it when `servers` is missing, but rename it to `servers`.
- Each server is started on its own. `startup_timeout` (seconds, default 30) is how long a server may take to start and answer before it is reported as timed out; set it at the top level for all servers or per server, for example for an `npx` package that downloads on first run. `timeout` (default 30) stays the limit for listing tools and for each tool call.
- A server that fails to start is reported with its name, reason and a hint, and skipped; the healthy servers are registered in ELITEA. `elitea-mcp serve` prints a table of all servers and the last lines a failed server wrote. A tool call to a server that cannot start is answered at once with a named error; after two such failures in a row the server is marked failed. On an ELITEA version that supports it, a server marked failed is also removed from ELITEA until it recovers, and a stateful server that announces new tools has them updated in ELITEA.
- `"$schema"` gives autocompletion and inline checks in editors that read JSON Schema (VS Code, JetBrains). The client writes `config.schema.json` for its installed version next to `config.json`.

## 🔄 Server Management

### Stateful vs Stateless Sessions

#### Stateless (Default)
- Each tool call creates a fresh connection
- Server state resets between calls
- Best for: File operations, API calls, simple commands

```json
{
  "servers": {
    "api_server": {
      "command": "python",
      "args": ["-m", "api_mcp_server"],
      "stateful": false
    }
  }
}
```

#### Stateful
- Maintains persistent connections
- Server state persists between calls
- Best for: Browser automation, database connections, complex workflows

```json
{
  "servers": {
    "browser_server": {
      "command": "python",
      "args": ["-m", "playwright_mcp_server"],
      "stateful": true
    }
  }
}
```

### Daemon Mode

Run servers in the background for continuous operation:

```bash
# Start daemon
elitea-mcp serve --daemon

# Check status
ps aux | grep "elitea-mcp serve"

# Stop daemon (find PID and kill)
kill $(cat ~/.local/var/run/elitea-mcp/elitea-mcp-serve.pid)
```

## 🖥️ Platform-Specific Notes

### macOS
- **Fork Safety**: Automatic handling of GUI framework compatibility
- **Dock Integration**: Tray app automatically hides from dock
- **Launch Agents**: Support for automatic startup
- **Homebrew**: Easy installation via pipx

### Linux
- **Desktop Entry**: Autostart support via `.desktop` files
- **System Tray**: Compatible with GNOME, KDE, and other desktop environments
- **Package Managers**: Works with apt, dnf, pacman via pip/pipx

### Windows
- **System Tray**: Native Windows system tray integration
- **Startup Folder**: Easy auto-start setup
- **PowerShell**: Full PowerShell compatibility

## 🛠️ Development

### Setup Development Environment

```bash
# Clone repository
git clone https://github.com/EliteaAI/elitea-mcp-client.git
cd elitea-mcp-client

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install in editable mode
pip install -e .

# Install development dependencies
pip install -e ".[dev]"
```

### Running Tests

```bash
# Run all tests
python -m pytest

# Run with coverage
python -m pytest --cov=elitea_mcp

# Run specific test file
python -m pytest tests/test_fork_safety.py -v
```

### Building and Publishing

```bash
# Install build tools
pip install build twine

# Build the package
python -m build

# Upload to PyPI (requires authentication)
twine upload dist/*
```

## 📚 Documentation

Comprehensive documentation is available in the `docs/` directory:

- **[TRAY_APPLICATION.md](docs/TRAY_APPLICATION.md)** - Complete tray app guide
- **[TRAY_SETUP_GUIDE.md](docs/TRAY_SETUP_GUIDE.md)** - Platform-specific setup
- **[STATEFUL_SESSIONS.md](docs/STATEFUL_SESSIONS.md)** - Server session management
- **[MACOS_FORK_SAFETY_FIX.md](docs/MACOS_FORK_SAFETY_FIX.md)** - macOS compatibility
- **[DAEMON_MODE_SUMMARY.md](docs/DAEMON_MODE_SUMMARY.md)** - Background operations

## 🤝 Contributing

We welcome contributions! Please follow these steps:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a Pull Request

### Development Guidelines

- Follow PEP 8 style guidelines
- Add tests for new features
- Update documentation as needed
- Ensure cross-platform compatibility

## 📝 License

This project is licensed under the Apache License 2.0 - see the [LICENSE](LICENSE) file for details.

## 🆘 Support

- **Documentation**: Check the `docs/` directory for detailed guides
- **Issues**: Report bugs and request features on [GitHub Issues](https://github.com/EliteaAI/elitea-mcp-client/issues)
- **Discussions**: Join community discussions on [GitHub Discussions](https://github.com/EliteaAI/elitea-mcp-client/discussions)

## 🔧 Troubleshooting

### Run the Doctor First

`elitea-mcp doctor` (or **Help → Run Diagnostics** in the tray) checks the client version against PyPI, the config file, the platform (URL, SSL, token, project access), starts every local server once to list its tools, and reports the SSE port and any client already running with the same config:

```text
ELITEA MCP doctor
  ✓ elitea-mcp 0.1.36: latest
  ✓ Config schema valid
  ✓ Token: accepted
  ✗ Local server "github" (stdio): command "npx" not found
      Install Node.js or use the full path in "command".
  ⚠ Running client: another client is connected with this config; servers were started a second time for this check, so a server that holds a port may fail here only
  ⚠ Running session: "jira" (stdio): declined (name already registered)

1 problem, 2 warnings.
```

- ✓ passed, ⚠ worth a look, ✗ broken. The exit code is 1 only when a line is ✗.
- The report never contains the token, header or `env` values, or a server's own output, and shows every URL as scheme and host only, since hosted MCP servers often carry a key in the URL path. When a failed server wrote output, its line ends with `Details: <file>`, the same masked `<server>-error.txt` the tray's **Servers** menu opens.
- The tray saves the report as `elitea-mcp-diagnostics.txt` next to the tray log, opens it and copies it to the clipboard, ready to paste into a support request.
- A running client (tray or `serve`) writes its server status to `status.json` in the config folder (owner-only), so the doctor can show the running session; a client that stops updating it for 60 s is reported as possibly frozen.

#### macOS: "Fork Safety" Errors
If you encounter fork safety errors on macOS, they have been automatically resolved in version 0.1.10+. The client now uses fork-safe daemon mode for GUI applications.

#### Tray Icon Not Appearing
- **Linux**: Ensure your desktop environment supports system tray icons
- **Windows**: Check if system tray icons are enabled in taskbar settings
- **macOS**: The icon should appear in the menu bar (top-right area)

#### Command Not Found
If `elitea-mcp` command is not found:
```bash
# For pipx installations
pipx ensurepath
source ~/.bashrc  # or ~/.zprofile for zsh

# For pip installations
pip install --user elitea-mcp
export PATH="$PATH:$HOME/.local/bin"
```

#### Configuration Issues
```bash
# Reset configuration
rm -rf ~/.config/elitea-mcp-client  # Linux
rm -rf ~/Library/Application\ Support/elitea-mcp-client  # macOS
# Then run: elitea-mcp bootstrap
```

### Getting Help

1. Check the relevant documentation in `docs/`
2. Search existing [GitHub Issues](https://github.com/EliteaAI/elitea-mcp-client/issues)
3. Create a new issue with:
   - The output of `elitea-mcp doctor` (or the tray's Run Diagnostics report)
   - Your operating system and version
   - Python version (`python --version`)
   - Full error message and traceback
   - Steps to reproduce the issue

## 🔄 Changelog

### Version 0.1.10 (Latest)

- ✅ **System Tray Application** - Complete GUI interface with context menus
- ✅ **macOS Fork Safety Fix** - Resolved all macOS GUI compatibility issues
- ✅ **Daemon Mode** - Background server operation with PID management
- ✅ **Cross-Platform Support** - Full Windows, macOS, and Linux compatibility
- ✅ **Auto-Start Integration** - System-level startup scripts and services
- ✅ **Enhanced Error Handling** - Comprehensive logging and recovery mechanisms
- ✅ **Configuration Management** - File-based config with GUI and CLI access
- ✅ **Production Ready** - Stable release with comprehensive testing

### Previous Versions

- **0.1.9** - Added basic tray functionality
- **0.1.8** - Improved server management
- **0.1.7** - Enhanced CLI interface
- **0.1.6** - Initial PyPI release

---

**[⬆ Back to Top](#elitea-mcp-client)**

Made with ❤️ by the Project EliteA Team

