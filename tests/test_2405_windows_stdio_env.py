"""A Windows stdio server receives the system folders it needs to find installed software, and none of the client's other variables."""
import asyncio
import json
import sys
import textwrap
from unittest.mock import patch

from src.elitea_mcp.utils import server_loader

ENV_SERVER = textwrap.dedent("""
    import json, os
    from mcp.server.fastmcp import FastMCP
    server = FastMCP("env")

    @server.tool()
    def read_env() -> str:
        return json.dumps(dict(os.environ))

    server.run()
""")
CLIENT_ENV = {
    "PROGRAMFILES": "C:\\Program Files",
    "PROGRAMFILES(X86)": "C:\\Program Files (x86)",
    "ELITEA_TOKEN": "client-secret-2405",
}


def build_env_server(directory, env=None):
    path = directory / "env_server.py"
    path.write_text(ENV_SERVER)
    return {"type": "stdio", "command": sys.executable, "args": [str(path)], **({"env": env} if env else {})}


def read_server_env(server_conf, on_windows):
    async def call_read_env():
        async with server_loader.open_session(server_conf, startup_timeout=10) as session:
            return json.loads((await session.call_tool("read_env", {})).content[0].text)

    with patch.object(server_loader, "ON_WINDOWS", on_windows), patch.dict(server_loader.os.environ, CLIENT_ENV):
        return asyncio.run(call_read_env())


def test_on_windows_a_stdio_server_receives_the_program_files_folders(tmp_path):
    server_env = read_server_env(build_env_server(tmp_path), on_windows=True)

    assert server_env["PROGRAMFILES"] == "C:\\Program Files"
    assert server_env["PROGRAMFILES(X86)"] == "C:\\Program Files (x86)"


def test_on_windows_the_clients_other_variables_still_stay_out_of_the_server(tmp_path):
    server_env = read_server_env(build_env_server(tmp_path), on_windows=True)

    assert "ELITEA_TOKEN" not in server_env


def test_on_windows_a_servers_own_env_wins_over_the_inherited_folders(tmp_path):
    server_conf = build_env_server(tmp_path, env={"PROGRAMFILES": "D:\\Apps", "API_KEY": "server-own"})

    server_env = read_server_env(server_conf, on_windows=True)

    assert server_env["PROGRAMFILES"] == "D:\\Apps"
    assert server_env["PROGRAMFILES(X86)"] == "C:\\Program Files (x86)"
    assert server_env["API_KEY"] == "server-own"


def test_off_windows_a_stdio_server_gets_the_same_environment_as_before(tmp_path):
    server_env = read_server_env(build_env_server(tmp_path), on_windows=False)

    assert "PROGRAMFILES" not in server_env
    assert "PROGRAMFILES(X86)" not in server_env
    assert "ELITEA_TOKEN" not in server_env
