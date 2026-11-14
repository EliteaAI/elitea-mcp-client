import re
import shlex

from ..config import get_config_file, update_config
from ..utils.name import sanitize


def bootstrap_servers(config):
    project_id = _prompt_int_or_empty("Enter Project ID if you need to add any mcp servers (or press Enter to skip): ")
    #
    if not project_id:
        return
    #
    servers = _servers_flow()

    def apply(config):
        config["project_id"] = project_id
        if not config.get("servers") and isinstance(config.get("mcpServers"), dict):
            config["servers"] = config.pop("mcpServers")
        config["servers"] = {**_read_existing_servers(config), **servers}
        return True

    update_config(apply, "The servers")
    print(f"Servers saved to {get_config_file()}")


def _read_existing_servers(config):
    """Keep the servers already configured, so running bootstrap again only adds or replaces the ones entered now."""
    servers = config.get("servers")
    return servers if isinstance(servers, dict) else {}


def _prompt_url(prompt):
    while True:
        value = input(prompt).strip()
        if re.match(r'^https?://', value) is not None:
            return value
        print("Invalid URL. Must start with http:// or https://")


def _prompt_nonempty(prompt):
    while True:
        if value := input(prompt).strip():
            return value
        print("Enter not empty value.")


def _prompt_int_or_empty(prompt):
    while True:
        value = input(prompt).strip()
        if value and value.isdigit():
            return int(value)
        elif not value:
            return value
        print("Enter a valid integer.")


def _prompt_int(prompt):
    while True:
        value = input(prompt).strip()
        if value.isdigit():
            return int(value)
        print("Enter a valid integer.")

def _servers_flow():
    servers = {}
    while True:
        name = input("Enter Server Name (the first symbol is letter and the rest are [a-zA-Z0-9_]) (leave blank to finish): ").strip()
        if not name:
            break
        else:
            name = sanitize(name)
        #
        server_type_number = _prompt_int("Enter Server Type (1 - sse or 2 - stdio): ")
        if server_type_number == 1:
            sse_url = _prompt_url("  SSE Url: ")
            servers[name] = {
                "type": "sse",
                "url": sse_url
            }
            if sse_token := input("  Enter Authorization Bearer Token (or leave blank to skip): ").strip():
                servers[name]["headers"] = {"Authorization": f"Bearer {sse_token}"}
        elif server_type_number == 2:
            command = _prompt_nonempty("  Enter Command: ")
            servers[name] = {
                "type": "stdio",
                "command": command
            }
            if args := input("  Enter Args (or leave blank to skip): "):
                servers[name]["args"] = shlex.split(args)
            
            # Ask about stateful behavior
            stateful_input = input("  Keep connection alive between tool calls? (y/n, default: n): ").strip().lower()
            if stateful_input in ['y', 'yes', 'true', '1']:
                servers[name]["stateful"] = True
        else:
            print("Only 1 or 2 is expected. Try again.")
    return servers