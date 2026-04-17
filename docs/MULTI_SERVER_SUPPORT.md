# Multi-Server Support in EliteA MCP Client

This document explains how the EliteA MCP Client supports multiple MCP servers with independent stateful/stateless behavior.

## Overview

The EliteA MCP Client can simultaneously manage multiple MCP servers, each with their own independent configuration and session management. Each server can be configured as either stateful (persistent sessions) or stateless (fresh connections).

## Key Features

### ✅ **Independent Server Configuration**
- Each server can have different `type` (stdio/sse), `command`, `args`, etc.
- Each server can independently be configured as `stateful: true` or `stateful: false`
- Mixed configurations are fully supported

### ✅ **Session Isolation**
- Each server maintains completely separate sessions
- No cross-contamination between server sessions
- Thread-safe session management with proper locking

### ✅ **Automatic Routing**
- Tool calls are automatically routed to the correct server
- Session manager uses `server_name` as unique key
- Proper error handling and fallback mechanisms

## Configuration Example

```json
{
  "deployment_url": "ws://localhost:8000",
  "auth_token": "your-token",
  "project_id": "your-project",
  "servers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": ["@executeautomation/playwright-mcp-server"],
      "stateful": true
    },
    "filesystem": {
      "type": "stdio", 
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "stateful": false
    },
    "brave-search": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-brave-search"],
      "stateful": true
    },
    "weather-api": {
      "type": "sse",
      "url": "https://weather-api.example.com/mcp",
      "headers": {"Authorization": "Bearer api-key"},
      "stateful": false
    }
  }
}
```

In this example:
- **playwright**: Stateful stdio server (browser stays open)
- **filesystem**: Stateless stdio server (fresh connection each time)
- **brave-search**: Stateful stdio server (persistent search context)
- **weather-api**: Stateless SSE server (fresh API calls)

## Architecture

### Session Management
```
SessionManager._sessions = {
    "playwright": (stdio_ctx, session_ctx, session),
    "brave-search": (stdio_ctx, session_ctx, session),
    # filesystem and weather-api not stored (stateless)
}
```

### Tool Call Flow
```
1. Socket.IO receives: {"server": "playwright", "params": {...}}
2. sio.py calls: _mcp_tools_call_sync(server_conf, params, "playwright")
3. Session manager checks: server_conf["stateful"] 
4. If stateful: Use/create persistent session with key "playwright"
5. If stateless: Create fresh connection for this call only
6. Tool result returned to Socket.IO
```

## Benefits

### 🚀 **Performance**
- Stateful servers avoid connection overhead
- Playwright browsers stay open between calls
- Database connections can be pooled

### 🔒 **Isolation**
- Server failures don't affect other servers
- Independent session lifecycles
- No shared state contamination

### 🎛️ **Flexibility**
- Mix stateful and stateless servers as needed
- Different server types (stdio/sse) in same config
- Per-server configuration granularity

## Runtime Behavior

### Server Startup
```
FILTERED SERVERS:
    [{'name': 'playwright', 'tools': [...]}, 
     {'name': 'filesystem', 'tools': [...]},
     {'name': 'brave-search', 'tools': [...]}]

Starting MCP client...
```

### Tool Execution
```
# Stateful server call
Received REQUEST: {"server": "playwright", "params": {...}}
TOOL RESULT (stateful session): [browser action result]

# Stateless server call  
Received REQUEST: {"server": "filesystem", "params": {...}}
TOOL RESULT (stateless session): [file operation result]
```

### Session Management
```python
# Check active sessions
session_manager.list_active_sessions()
# Returns: {"playwright": "stdio", "brave-search": "stdio"}
# Note: stateless servers not listed
```

## Best Practices

### 🎯 **Server Selection**
- Use **stateful** for: Playwright, databases, interactive shells
- Use **stateless** for: File operations, API calls, simple commands

### 📊 **Configuration Strategy**
- Start with stateless (safer, less memory)
- Enable stateful only when needed for workflow continuity
- Monitor memory usage with multiple stateful servers

### 🔧 **Error Handling**
- Stateful sessions auto-recover on failure
- Automatic fallback to stateless mode
- Independent error isolation per server

## Troubleshooting

### Multiple Server Issues
1. **Config loading errors**: Check JSON syntax for all servers
2. **Session conflicts**: Each server uses independent sessions
3. **Memory usage**: Consider reducing stateful servers
4. **Tool routing**: Verify `server_name` parameter is correct

### Verification Commands
```bash
# Test multi-server configuration
python3 -c "from src.elitea_mcp.config import load_config; print(load_config()['servers'].keys())"

# Check session manager support  
python3 -c "from src.elitea_mcp.utils.session_manager import get_session_manager; print('Multi-server ready!')"
```

## Conclusion

The EliteA MCP Client provides robust multi-server support with:
- ✅ Independent stateful/stateless configuration per server
- ✅ Thread-safe session isolation and management  
- ✅ Automatic routing and error recovery
- ✅ Support for mixed server types (stdio/sse)
- ✅ Comprehensive cleanup and lifecycle management

This architecture enables complex workflows that combine different types of MCP servers while maintaining optimal performance and reliability.
