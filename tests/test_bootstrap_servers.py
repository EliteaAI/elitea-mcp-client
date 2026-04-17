import builtins
from src.elitea_mcp.commands.bootstrap_servers import _servers_flow


def test_servers_flow_args_parsing(monkeypatch):
    inputs = iter([
        "playwright",  # server name
        "2",  # server type stdio
        "npx",  # command
        "@playwright/mcp@latest",  # args string
        "n",  # stateful prompt
        ""  # end loop
    ])

    monkeypatch.setattr(builtins, "input", lambda _: next(inputs))
    servers = _servers_flow()
    assert servers["playwright"]["args"] == ["@playwright/mcp@latest"]
