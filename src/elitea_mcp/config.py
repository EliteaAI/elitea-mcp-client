import json
import os
import logging
import shutil
import tempfile
from pathlib import Path

from .config_errors import ConfigError, ConfigMistake, backup_file_for, parse_config
from .utils.package_updates import DEFAULT_CHECK_INTERVAL_HOURS
from appdirs import user_config_dir

APP_NAME = "elitea-mcp-client"
SCHEMA_FILE_NAME = "config.schema.json"
SCHEMA_REFERENCE = f"./{SCHEMA_FILE_NAME}"

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

def load_config(config_file=None):
    """Load the config exactly as written, raising ConfigError when it cannot be parsed; config_model.check_config resolves it."""
    config_file = config_file or get_config_file()
    if not hasattr(config_file, "exists"):
        from pathlib import Path
        config_file = Path(config_file)
    if not config_file.exists():
        return {}
    
    with open(str(config_file), "r", encoding="utf-8-sig") as f:
        return parse_config(read_config_text(f, config_file), config_file)

def read_config_text(config_stream, config_file):
    try:
        return config_stream.read()
    except UnicodeDecodeError:
        raise ConfigError(config_file, "Config file is not UTF-8 text", "The file uses another text encoding",
                          ConfigMistake.NOT_UTF8) from None


def is_ssl_verification_enabled(config):
    """Decide TLS verification for platform calls; the environment overrides the config, which defaults to off for corporate proxies."""
    env_setting = os.environ.get('ELITEA_DISABLE_SSL_VERIFY', '').lower()
    if env_setting in ('true', 'false'):
        return env_setting == 'false'
    return bool(config.get("ssl_verify", False))

def save_config(config):
    """Replace the config file in one step so a failed write never leaves it partial, keeping the previous valid version as a backup."""
    config_file = Path(str(get_config_file()))
    backup_valid_config(config_file)
    replace_file(config_file, json.dumps(config, indent=2))


def replace_file(path, text):
    """Write text to path through a temporary file and one rename, so readers never see a partial file."""
    descriptor, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise


def backup_valid_config(config_file):
    if not config_file.exists():
        return
    try:
        load_config(config_file)
    except ConfigError:
        return
    shutil.copyfile(config_file, backup_file_for(config_file))

def set_bootstrap_config(deployment_url, auth_token, host='0.0.0.0', port=8000):
    """Set bootstrap configuration values."""
    config = load_config() if get_config_file().exists() else {"$schema": SCHEMA_REFERENCE}
    config["deployment_url"] = deployment_url
    config["auth_token"] = auth_token
    config["host"] = host
    config["port"] = port
    save_config(config)
    return config

def get_bootstrap_config(config=None):
    """Get bootstrap configuration values, from the given parsed config or from the file."""
    config = load_config() if config is None else config
    
    return {
        "deployment_url": config.get("deployment_url"),
        "auth_token": config.get("auth_token"),
        "host": config.get("host", "0.0.0.0"),
        "port": config.get("port", 8000),
        "project_id": config.get("project_id", None),
        "servers": config.get("servers", {}),
        "ssl_verify": config.get("ssl_verify", False),
        "update_check": config.get("update_check", True),
        "update_check_interval_hours": config.get("update_check_interval_hours", DEFAULT_CHECK_INTERVAL_HOURS),
    }

def read_port(current_port):
    """Ask for the port and keep it a number, so the saved config holds 8080 and not "8080"."""
    typed = input(f"Port (default: {current_port}): ").strip()
    if typed.isdigit():
        return int(typed)
    if typed:
        print(f'"{typed}" is not a port number; keeping {current_port}.')
    return current_port

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
    new_port = read_port(current_port)
    
    # Use new values or keep existing ones if input was empty
    deployment_url = new_url if new_url else current_url
    auth_token = new_token if new_token else current_token
    
    
    # Only save if we have values
    if deployment_url or auth_token:
        return set_bootstrap_config(deployment_url, auth_token, new_host, new_port)
    else:
        print("Configuration unchanged.")
        return config
