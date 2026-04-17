@echo off
REM Windows Launch Script for EliteA MCP Tray Application

REM Check if elitea-mcp is available
where elitea-mcp >nul 2>nul
if %errorlevel% == 0 (
    elitea-mcp tray
) else (
    echo elitea-mcp not found. Please install elitea-mcp first:
    echo pip install elitea-mcp
    pause
)
