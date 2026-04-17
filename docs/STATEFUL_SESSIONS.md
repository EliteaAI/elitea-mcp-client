# Stateful MCP Server Sessions

This document explains how to use the stateful session management feature in the EliteA MCP Client.

## Overview

By default, MCP servers operate in **stateless** mode. Each tool call creates a new connection to the server, executes the command, and then closes the connection. This is ideal for most use cases but can be problematic for servers like Playwright that need to maintain browser state between commands.

The **stateful** mode allows servers to maintain persistent connections, keeping their state (like open browsers) alive between tool calls.

## Configuration

### During Bootstrap

When running `elitea-mcp bootstrap-servers`, you'll be prompted to configure servers. For stdio servers, you'll see:

```
Keep connection alive between tool calls? (y/n, default: n):
```
When entering the command arguments, provide them as a single line separated by
spaces. The client will automatically split them into a list in the stored
configuration.

- Answer `y`, `yes`, `true`, or `1` to enable stateful mode
- Answer `n`, `no`, `false`, `0`, or press Enter to use stateless mode (default)

### Manual Configuration

You can also manually edit your configuration file to add the `stateful` flag:

```json
{
  "servers": {
    "playwright": {
      "type": "stdio",
      "command": "npx",
      "args": ["@modelcontextprotocol/server-playwright"],
      "stateful": true
    },
    "filesystem": {
      "type": "stdio", 
      "command": "npx",
      "args": ["@modelcontextprotocol/server-filesystem", "/tmp"]
    }
  }
}
```

In this example:
- `playwright` server will maintain persistent sessions (browser stays open)
- `filesystem` server uses default stateless behavior

## Behavior

### Stateless Mode (Default)
- Each tool call creates a fresh connection
- Server state is reset between calls
- Best for: File operations, API calls, simple commands
- Memory efficient

### Stateful Mode  
- Single persistent connection is maintained
- Server state persists between calls
- Best for: Playwright (browser automation), database connections, interactive sessions
- Uses more memory but enables stateful workflows

## Example Use Cases

### Playwright Web Automation

**Stateless** (not recommended for Playwright):
```
Tool Call 1: Navigate to website → Browser opens → Browser closes
Tool Call 2: Click button → New browser opens → Cannot find the website → Fails
```

**Stateful** (recommended for Playwright):
```
Tool Call 1: Navigate to website → Browser opens → Browser stays open
Tool Call 2: Click button → Uses same browser session → Success!
Tool Call 3: Fill form → Uses same browser session → Success!
```

### Error Recovery

The stateful session manager includes automatic error recovery:

1. If a tool call fails, it will automatically recreate the session
2. Up to 2 retry attempts are made before falling back to stateless mode
3. This ensures robustness even with persistent connections

## Session Management

### Viewing Active Sessions

When you start the MCP client, it will show which servers are running in stateful mode:

```
STATEFUL SERVERS (persistent sessions): ['playwright']
These servers will maintain their state between tool calls.
```

### Cleanup

Sessions are automatically cleaned up when:
- The application exits (Ctrl+C)
- A session encounters an unrecoverable error
- The application shuts down normally

## Configuration File Location

Your configuration is stored in:
- macOS: `~/Library/Application Support/elitea-mcp-client/config.json`
- Linux: `~/.config/elitea-mcp-client/config.json`  
- Windows: `%APPDATA%/elitea-mcp-client/config.json`

## Best Practices

1. **Use stateful mode sparingly**: Only enable it for servers that truly need persistent state
2. **Monitor memory usage**: Stateful sessions consume more memory
3. **Test both modes**: Verify your workflows work with the chosen mode
4. **Handle session failures**: The client will fallback to stateless mode if stateful fails

## Troubleshooting

### Session Creation Failures
If a stateful session fails to create, the client will:
1. Log the error
2. Fall back to stateless mode for that tool call
3. Continue operating normally

### Memory Issues
If you experience memory issues with stateful sessions:
1. Reduce the number of stateful servers
2. Consider if stateless mode would work for your use case
3. Restart the client periodically to clean up sessions

### Browser Not Closing
For Playwright servers in stateful mode, browsers will remain open until:
1. The application is shut down
2. The session is manually reset
3. An error forces session recreation

This is expected behavior for stateful mode.
