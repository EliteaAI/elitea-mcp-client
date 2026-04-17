import json
import os
import logging
from pathlib import Path

from .utils.name import sanitize
from appdirs import user_config_dir

APP_NAME = "elitea-mcp-client"

# Configure logger
logger = logging.getLogger(__name__)

def get_config_dir():
    """Return the configuration directory for the application."""
    config_dir = Path(user_config_dir(APP_NAME))
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir

def get_config_file():
    """Return the path to the configuration file."""
    return get_config_dir() / "config.json"

def load_config():
    """Load configuration from the config file."""
    config_file = get_config_file()
    if not hasattr(config_file, "exists"):
        from pathlib import Path
        config_file = Path(config_file)
    if not config_file.exists():
        return {}
    
    with open(str(config_file), "r") as f:
        config = json.load(f)

        servers = config.get("servers", {})
        sanitized_servers = {sanitize(name): value for name, value in servers.items()}
        config["servers"] = sanitized_servers

        try:
            config['timeout'] = int(config.get('timeout', 30))
        except (ValueError, TypeError):
            config['timeout'] = 30
        
        # Set default SSL configuration for corporate environments
        if 'ssl_verify' not in config:
            config['ssl_verify'] = False
        
        return config

def save_config(config):
    """Save configuration to the config file."""
    with open(get_config_file(), "w") as f:
        json.dump(config, f, indent=2)

def set_bootstrap_config(deployment_url, auth_token, host='0.0.0.0', port=8000):
    """Set bootstrap configuration values."""
    config = load_config()
    config["deployment_url"] = deployment_url
    config["auth_token"] = auth_token
    config["host"] = host
    config["port"] = port
    save_config(config)
    return config

def get_bootstrap_config():
    """Get bootstrap configuration values."""
    config = load_config()
    
    return {
        "deployment_url": config.get("deployment_url"),
        "auth_token": config.get("auth_token"),
        "host": config.get("host", "0.0.0.0"),
        "port": config.get("port", 8000),
        "project_id": config.get("project_id", None),
        "servers": config.get("servers", {}),
        "ssl_verify": config.get("ssl_verify", False),
    }

def interactive_bootstrap():
    """
    Interactive bootstrap configuration by prompting user for values.
    Returns the updated configuration.
    """
    config = load_config()
    current_url = config.get("deployment_url", "")
    current_token = config.get("auth_token", "")
    current_host = config.get("host", "0.0.0.0")
    current_port = config.get("port", 8000)
    
    # Show current values if they exist
    if current_url:
        print(f"Current deployment URL: {current_url}")
    if current_token:
        masked_token = "********" + current_token[-4:] if len(current_token) > 4 else "********"
        print(f"Current auth token: {masked_token}")
    
    # Prompt for new values
    print("\nEnter new values (or press Enter to keep current values):")
    new_url = input(f"Deployment URL: ")
    new_token = input(f"Authentication token: ")
    new_host = input(f"Host (default: {current_host}): ") or current_host
    new_port = input(f"Port (default: {current_port}): ") or current_port
    
    # Use new values or keep existing ones if input was empty
    deployment_url = new_url if new_url else current_url
    auth_token = new_token if new_token else current_token
    
    
    # Only save if we have values
    if deployment_url or auth_token:
        return set_bootstrap_config(deployment_url, auth_token, new_host, new_port)
    else:
        print("Configuration unchanged.")
        return config
