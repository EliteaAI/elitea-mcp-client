# SSL Certificate Fix for ELITEA MCP Client

## Issue Description
The ELITEA MCP client was failing to connect to instance with ssl issues due to SSL certificate verification errors:
- `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate`
- `Connection reset by peer`

This is a common issue in corporate environments where internal certificates might not be recognized by the default Python SSL certificate store.

## Solution Implemented

### 1. Modified SocketIO Client Configuration
**File: `src/elitea_mcp/utils/sio.py`**

- Added SSL context configuration that disables certificate verification
- Added support for both environment variable and config file control
- Implemented proper error handling and user feedback

Key changes:
```python
# Check if SSL verification should be disabled (useful for corporate environments)
# Priority: environment variable > config file > default (disabled for corporate environments)
env_ssl_setting = os.environ.get('ELITEA_DISABLE_SSL_VERIFY', '').lower()
if env_ssl_setting in ('true', 'false'):
    disable_ssl_verify = env_ssl_setting == 'true'
else:
    disable_ssl_verify = not config.get("ssl_verify", False)

if disable_ssl_verify:
    # Create SocketIO client with SSL verification disabled
    sio = socketio.Client(ssl_verify=False, logger=False, engineio_logger=False)
    print("SSL verification disabled for SocketIO connection")
else:
    sio = socketio.Client()
    print("SSL verification enabled for SocketIO connection")
```

### 2. Enhanced Configuration Support
**File: `src/elitea_mcp/config.py`**

- Added `ssl_verify` configuration option (defaults to `false` for corporate environments)
- Updated configuration loading and bootstrap functions

### 3. Updated User Configuration
**File: `/Users/Alexander_Bychinskiy/Library/Application Support/elitea-mcp-client/config.json`**

- Added `"ssl_verify": false` to your existing configuration

## Configuration Options

You can control SSL verification through multiple methods (in order of priority):

### Method 1: Environment Variable (Highest Priority)
```bash
export ELITEA_DISABLE_SSL_VERIFY=true   # Disable SSL verification
export ELITEA_DISABLE_SSL_VERIFY=false  # Enable SSL verification
```

### Method 2: Configuration File
In your `config.json` file:
```json
{
  "ssl_verify": false,  // Disable SSL verification
  "ssl_verify": true    // Enable SSL verification
}
```

### Method 3: Default Behavior
If neither environment variable nor config file specifies the setting, SSL verification is disabled by default (suitable for corporate environments).

## Testing Instructions

### 1. Test SSL Connection (Optional)
First, run the SSL connection test to understand the issue:
```bash
cd /Users/Alexander_Bychinskiy/GitHub/EliteaAI/elitea-mcp-client
python test_ssl_connection.py
```

This will show you whether the SSL issue is with verification or something else.

### 2. Test the Fixed MCP Client
Try running the MCP client with either command:

**Option A: Using the original command format (recommended):**
```bash
elitea-mcp run --project_id 5
```

**Option B: Using the serve command:**
```bash
elitea-mcp serve
```

You should see:
- `SSL verification disabled for SocketIO connection`
- Successful connection to the platform

### 3. VS Code MCP Configuration
Update your VS Code MCP configuration (`mcp.json`) to use the fixed client:

```json
{
  "servers": {
    "ELITEA MCP": {
      "type": "stdio",
      "command": "elitea-mcp",
      "args": ["run", "--project_id", "5"]
    }
  }
}
```

### 3. If Still Having Issues
If you still encounter connection problems, try:

#### Option A: Force SSL Verification Disabled
```bash
export ELITEA_DISABLE_SSL_VERIFY=true
elitea-mcp run --project_id 5
```

#### Option B: Rebuild and Install the Package
If changes aren't taking effect, rebuild the package:
```bash
cd /Users/Alexander_Bychinskiy/GitHub/EliteaAI/elitea-mcp-client
pip3 uninstall elitea-mcp -y
python3 -m build
pip3 install dist/elitea_mcp-0.1.25-py3-none-any.whl
```

## Security Considerations

**Important:** Disabling SSL verification reduces security by making the connection vulnerable to man-in-the-middle attacks. This solution is intended for:
- Corporate environments with internal certificates
- Development/testing environments
- Situations where the certificate issue cannot be resolved at the system level

For production environments, consider:
1. Installing the proper corporate root certificates in your system's certificate store
2. Updating the Python certificate bundle
3. Using a custom certificate bundle path

## Files Modified

1. **`src/elitea_mcp/utils/sio.py`** - Main SocketIO client configuration with SSL bypass
2. **`src/elitea_mcp/config.py`** - Configuration loading and SSL settings management
3. **`config.json`** - User configuration file (updated with `ssl_verify: false`)
4. **`test_ssl_connection.py`** - SSL connection testing utility (new file)
5. **`run_elitea_mcp_fixed.sh`** - Backup script for running fixed version (new file)

## Installation Method Used

The final working solution used the following installation approach:
```bash
# Build the package with fixes
cd /Users/Alexander_Bychinskiy/GitHub/EliteaAI/elitea-mcp-client
python3 -m build

# Install the built package
pip3 uninstall elitea-mcp -y
pip3 install dist/elitea_mcp-0.1.25-py3-none-any.whl
```

This ensures the SSL fixes are properly included in the installed package.

## Final Working Commands

After applying the fixes, you can use the ELITEA MCP client in the following ways:

### 1. Direct Command Line Usage
```bash
# Run as MCP server (for VS Code integration)
elitea-mcp run --project_id 5

# Run as standalone client (connects to platform)
elitea-mcp serve
```

### 2. VS Code MCP Integration
```json
{
  "servers": {
    "ELITEA MCP": {
      "type": "stdio",
      "command": "elitea-mcp",
      "args": ["run", "--project_id", "5"]
    }
  }
}
```

### 3. Available MCP Tools
Once configured, you'll have access to:
- **Module Source Code Reader** - Analyzes service endpoint implementations
- **Related Domain Service Endpoint Extractor** - Maps DXP endpoints to domain services
- **Data Miner Sample** - Extracts test data from automation frameworks

## Verification

After applying these fixes, you should be able to:
1. Connect to the ELITEA platform without SSL certificate errors
2. See "SSL verification disabled for SocketIO connection" in the output
3. Successfully establish a SocketIO connection to  instance with ssl issues
4. Use ELITEA MCP tools in VS Code through the MCP extension

The MCP client should now work properly in your corporate environment.
