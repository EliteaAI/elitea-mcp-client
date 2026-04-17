# macOS Fork Safety Fix for EliteA MCP Tray Application

## Issue Description

On macOS, the tray application was experiencing crashes with the following error:
```
objc[94117]: +[NSResponder initialize] may have been in progress in another thread when fork() was called.
objc[94117]: +[NSResponder initialize] may have been in progress in another thread when fork() was called. We cannot safely call it or ignore it in the fork() child process. Crashing instead.
```

## Root Cause

This is a classic macOS fork safety issue that occurs when:

1. A Python application imports GUI frameworks (like `pystray`, which loads Cocoa/AppKit)
2. Later, the application tries to use `subprocess.Popen()` or `multiprocessing` with fork
3. macOS's Objective-C frameworks are not fork-safe after initialization

## Solution Implemented

### 1. Environment Variable Fix
Added `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` environment variable:
- Set in the tray application code before importing pystray
- Set in the server controller before subprocess operations
- Set in launch scripts for macOS

### 2. Multiprocessing Start Method
Set multiprocessing start method to 'spawn' instead of 'fork' on macOS:
```python
if platform.system() == "Darwin":
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass  # Already set
```

### 3. Subprocess Safety Enhancements
Modified subprocess creation to use safer parameters on macOS:
- Added `start_new_session=True` for daemon processes
- Passed environment variables with fork safety disabled
- Used `env` parameter to ensure proper environment propagation

### 4. Launch Scripts Updated
Created macOS-specific launch scripts that set the environment variable:
- `scripts/launch-tray-macos.sh` - Main launcher with fix
- `scripts/launch-tray-macos-safe.sh` - Alternative safe launcher

## Files Modified

1. **src/elitea_mcp/tray.py**
   - Added fork safety environment variable
   - Set multiprocessing start method to spawn
   - Enhanced `_open_file()` method for macOS subprocess safety

2. **src/elitea_mcp/utils/server_controller.py**
   - Added fork safety environment variable
   - Enhanced `_start_daemon_servers()` with safer subprocess creation
   - Added platform-specific subprocess parameters

3. **scripts/launch-tray-macos.sh**
   - New launcher script with fork safety fix

4. **scripts/launch-tray-macos-safe.sh**
   - Alternative safe launcher script

## Testing

The fix has been tested and verified:
```bash
# Test import without crashes
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
python -c "from src.elitea_mcp.tray import MCPTrayApp; print('✅ Success')"

# Test subprocess after tray import
python -c "
from src.elitea_mcp.tray import MCPTrayApp
import subprocess
result = subprocess.run(['echo', 'test'], capture_output=True, text=True)
print('✅ Subprocess working:', result.stdout.strip())
"

# Test actual tray application
elitea-mcp tray  # Should start without fork safety crashes
```

**Test Results (Verified ✅):**
- ✅ Tray application imports successfully
- ✅ No fork safety crashes on startup
- ✅ Subprocess operations work correctly after GUI initialization
- ✅ Console output shows: "Successfully hidden from macOS dock" and tray instructions
- ✅ Launch scripts work correctly

## Usage

### Method 1: Use Launch Script
```bash
./scripts/launch-tray-macos.sh
```

### Method 2: Set Environment Variable
```bash
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES
elitea-mcp-tray
```

### Method 3: Command Line
```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES elitea-mcp-tray
```

## Notes

- This fix is specific to macOS and does not affect other platforms
- The environment variable disables Apple's fork safety check, which is safe for our use case
- The spawn method for multiprocessing is more resource-intensive but avoids fork issues
- All subprocess operations now properly handle the macOS fork safety requirements

## References

- Apple Documentation: Fork Safety and Objective-C
- Python multiprocessing documentation
- pystray and macOS compatibility notes
