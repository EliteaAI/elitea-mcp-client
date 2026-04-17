#!/bin/bash
# macOS Tray Application Launcher with Fork Safety
# This script properly handles the macOS fork safety issue with GUI applications

# Set fork safety environment variable
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

# Set multiprocessing method to spawn
export PYTHONPATH="$PYTHONPATH"

# Launch the tray application
exec elitea-mcp tray
