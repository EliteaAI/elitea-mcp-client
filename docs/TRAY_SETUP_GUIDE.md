# EliteA MCP Client Tray Application Setup Guide

This guide will help you set up and use the EliteA MCP Client tray application.

## Installation

### Step 1: Install Dependencies

```bash
# Install the required dependencies
pip install pystray pillow

# On macOS, tkinter should be included with Python
# On Linux, you might need to install tkinter separately:
# sudo apt-get install python3-tk  # Ubuntu/Debian
# sudo yum install tkinter          # CentOS/RHEL
```

### Step 2: Verify Installation

```bash
# Test that the tray module can be imported
python -c "from elitea_mcp.tray import run_tray; print('Tray application ready!')"
```

## Basic Usage

### Starting the Tray Application

```bash
# Method 1: Using the CLI command
elitea-mcp tray

# Method 2: Using the dedicated script
elitea-mcp-tray

# Method 3: Running directly
python -m elitea_mcp.tray
```

### First Time Setup

1. **Start the tray application** using one of the methods above
2. **Right-click the tray icon** (blue circle with "MCP" text)
3. **Select "Configuration" > "Edit Config"** or "Bootstrap"
4. **Enter your configuration**:
   - Deployment URL: Your EliteA deployment URL
   - Auth Token: Your authentication token
   - Host: Usually `0.0.0.0` (default)
   - Port: Usually `8000` (default)
5. **Click "Save"**

### Starting MCP Servers

Once configured:
1. **Right-click the tray icon**
2. **Select "Start MCP Server"**
3. **Monitor notifications** for status updates

### Stopping MCP Servers

1. **Right-click the tray icon**
2. **Select "Stop MCP Server"**

### Restarting MCP Servers

1. **Right-click the tray icon**
2. **Select "Restart MCP Server"** to reload configuration

## Platform-Specific Setup

### macOS

#### Auto-start with Login
1. Copy the launcher script:
   ```bash
   cp scripts/launch-tray-macos.sh ~/Applications/
   chmod +x ~/Applications/launch-tray-macos.sh
   ```

2. Add to Login Items:
   - Open **System Preferences** > **Users & Groups**
   - Click your user account
   - Click **Login Items** tab
   - Click the **+** button
   - Navigate to and select `launch-tray-macos.sh`

#### Using LaunchAgent (Advanced)
```bash
# Copy the launch agent template
cp scripts/com.elitea.mcp.tray.plist ~/Library/LaunchAgents/

# Edit the file to update paths
# Load the launch agent
launchctl load ~/Library/LaunchAgents/com.elitea.mcp.tray.plist
```

### Linux

#### Desktop Entry
```bash
# Copy desktop entry
cp scripts/elitea-mcp-tray.desktop ~/.local/share/applications/

# Update Exec path in the file to match your installation
# Make executable
chmod +x ~/.local/share/applications/elitea-mcp-tray.desktop
```

#### Auto-start
```bash
# Copy to autostart directory
cp scripts/elitea-mcp-tray.desktop ~/.config/autostart/
```

### Windows

#### Startup Folder
1. Press `Win + R`, type `shell:startup`, press Enter
2. Copy `scripts/launch-tray-windows.bat` to this folder
3. Edit the batch file to update Python path if needed

#### Task Scheduler (Advanced)
1. Open **Task Scheduler**
2. Create **Basic Task**
3. Set trigger to **"When I log on"**
4. Set action to start `launch-tray-windows.bat`

## Troubleshooting

### Tray Icon Not Appearing
- **Linux**: Install a system tray extension for your desktop environment
- **Windows**: Check that system tray icons are enabled
- **macOS**: Ensure the application has accessibility permissions

### GUI Not Working
If tkinter is not available, the tray will still work but configuration editing will be limited to command line:
```bash
# Use command line for configuration
elitea-mcp bootstrap
```

### Server Won't Start
1. **Check configuration**: Right-click tray icon > "Configuration" > "View Current Config"
2. **Verify servers**: Ensure MCP servers are properly configured
3. **Check connectivity**: Verify network access to deployment URL
4. **Review logs**: Check terminal output for error messages

### Performance Issues
- The tray application is lightweight and should use minimal resources
- If experiencing issues, restart the tray application
- Check for conflicting applications using the system tray

## Features Reference

### Menu Items
- **EliteA MCP Client** (header, disabled)
- **Start/Stop MCP Server** (when configured)
- **Restart MCP Server**
- **Configuration**
  - **Edit Config**: GUI dialog for editing settings
  - **Bootstrap**: Configuration wizard
  - **View Current Config**: Display current settings
- **About**: Application information
- **Quit**: Exit application

### Keyboard Shortcuts
- No specific keyboard shortcuts (operated via mouse/trackpad)
- Use system tray keyboard navigation if supported by your OS

### Notifications
The tray application shows notifications for:
- Server start/stop events
- Configuration changes
- Error conditions
- Status updates

## Development

### Running in Development Mode
```bash
# From the project root
python -m src.elitea_mcp.tray
```

### Testing
```bash
# Run the demo script
python demo_tray.py

# Test specific components
python -c "from src.elitea_mcp.tray import MCPTrayApp; app = MCPTrayApp(); print('Test passed')"
```

### Building Icons
The tray application generates its icon dynamically using PIL/Pillow. You can customize the icon by modifying the `create_icon_image()` method in `src/elitea_mcp/tray.py`.

## Security Considerations

- **Authentication tokens** are masked in the UI for security
- **Configuration files** are stored in user-specific directories
- **Network communication** uses HTTPS when properly configured
- **No sensitive data** is logged to console in production mode

## Support

For issues and questions:
1. Check this documentation
2. Review the main README.md
3. Check the issues page on GitHub
4. Use command line alternatives if GUI features are not working
