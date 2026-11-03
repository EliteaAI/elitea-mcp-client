import asyncio
import sys

from ..config import load_config
from ..utils.menu import show_menu
from ..utils.preflight import PreflightError, validate_platform_config
from ..utils.sio import PlatformConnectionError, start_socket_connection, get_all_tools


def run_servers():
    try:
        config = validate_platform_config(load_config())
    except PreflightError as error:
        print(f"Cannot connect to platform: {error}")
        sys.exit(1)
    print("\nMcp Servers from configuration:")
    print(", ".join(config["servers"].keys()))
    print("\nConnecting to the servers...")
    all_tools = asyncio.run(get_all_tools(config["servers"]))
    # tree = {}
    # for server in all_tools:
    #     tools = [tool["name"] for tool in server.get("tools", [])]
    #     tree[server["name"]] = tools

    # print result
    print("\nAvailable Mcp Servers and Tools to serve:")
    for server in all_tools:
        tools = server["tools"]
        print(f"\n  Mcp Server: {server['name']}")
        print(f"    {len(tools)} tools available:")
        print(f"    {', '.join([tool['name'] for tool in tools])}")

    try:
        # show_menu(tree)
        print(f"\nStarting MCP client...")
        try:
            start_socket_connection(config, all_tools)
        except PlatformConnectionError as error:
            print(f"Cannot connect to platform: {error}")
            sys.exit(1)
        while True:
            user_input = input("").strip()
            
    except KeyboardInterrupt:
        print("\nExiting application...")
        sys.exit(0)