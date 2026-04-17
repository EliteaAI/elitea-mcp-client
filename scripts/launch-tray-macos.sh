#!/bin/bash
# macOS Launch Script for EliteA MCP Tray Application
# Includes fix for macOS fork safety issue

# Set fork safety environment variable to prevent crashes
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# Check if elitea-mcp is available
if command -v elitea-mcp &> /dev/null; then
    elitea-mcp tray
else
    echo "elitea-mcp not found. Please install elitea-mcp first:"
    echo "pip install elitea-mcp"
fi
