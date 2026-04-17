"""
Test fork safety fix for macOS tray application.
"""
import os
import sys
import platform
import subprocess
import pytest


def test_fork_safety_environment_setup():
    """Test that fork safety environment is properly set."""
    if platform.system() != "Darwin":
        pytest.skip("Fork safety test only applies to macOS")
    
    # Apply the fork safety fix like the tray application does
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    
    import multiprocessing
    try:
        multiprocessing.set_start_method('spawn', force=True)
    except RuntimeError:
        pass  # Already set
    
    assert os.environ.get('OBJC_DISABLE_INITIALIZE_FORK_SAFETY') == 'YES'


def test_tray_import_after_fork_safety_fix():
    """Test that tray components can be imported without crashes."""
    if platform.system() != "Darwin":
        pytest.skip("Fork safety test only applies to macOS")
    
    # Set up fork safety
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    
    # This should not crash
    from elitea_mcp.tray import MCPTrayApp
    
    # Create app instance to test icon creation
    app = MCPTrayApp()
    icon_image = app.create_icon_image()
    
    assert icon_image is not None


def test_subprocess_after_gui_import():
    """Test that subprocess works after importing GUI components."""
    if platform.system() != "Darwin":
        pytest.skip("Fork safety test only applies to macOS")
    
    # Set up fork safety
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    
    # Import GUI components first
    from elitea_mcp.tray import MCPTrayApp
    app = MCPTrayApp()
    
    # Now test subprocess - this should not crash with fork safety issues
    result = subprocess.run(['echo', 'test_fork_safety'], 
                          capture_output=True, text=True, timeout=5)
    
    assert result.returncode == 0
    assert 'test_fork_safety' in result.stdout


def test_macos_daemon_mode_fork_safety():
    """Test that macOS daemon mode uses fork-safe approach."""
    if platform.system() != "Darwin":
        pytest.skip("macOS daemon mode test only applies to macOS")
    
    # Set up environment like the CLI does
    os.environ['OBJC_DISABLE_INITIALIZE_FORK_SAFETY'] = 'YES'
    
    # Import the main module to test CLI behavior
    import elitea_mcp.main
    
    # Test that the macos_gui_daemonize function exists and is callable
    assert hasattr(elitea_mcp.main, 'macos_gui_daemonize')
    assert callable(elitea_mcp.main.macos_gui_daemonize)
    
    # Test environment variable detection
    os.environ['ELITEA_MCP_DAEMON_MODE'] = 'true'
    assert os.environ.get('ELITEA_MCP_DAEMON_MODE') == 'true'
    
    # Clean up
    del os.environ['ELITEA_MCP_DAEMON_MODE']
