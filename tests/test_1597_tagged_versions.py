"""#1597: the local client serves the agent versions the platform exposes over MCP (the ones tagged ``mcp``), each
under the tool name the platform gives it, and a call runs exactly that version. Against a platform that does not
report exposed versions yet it keeps serving the newest version of each tagged agent."""
import asyncio
import logging

import mcp.types as types
import pytest

from src.elitea_mcp.clients import elitea
from src.elitea_mcp.clients.elitea import Agent, Agents, ApiDetailsRequestError
from src.elitea_mcp.server.mcp import create_server

BASE = "http://platform.test/api/v2/elitea_core"
HELPER = {"id": 1, "name": "Helper", "description": "does things"}
VERSIONS = {10: {"id": 10, "variables": []}, 11: {"id": 11, "variables": [{"name": "tone", "value": "dry"}]}}
EXPOSED_ROWS = [
    {"application_id": 1, "application_name": "Helper", "version_id": 10, "version_name": "base",
     "tool_name": "Helper", "description": "does things", "rule": "tagged"},
    {"application_id": 1, "application_name": "Helper", "version_id": 11, "version_name": "v2",
     "tool_name": "Helper_v2", "description": "Version 'v2' of agent 'Helper'. does things", "rule": "tagged"},
]


class Response:
    def __init__(self, status_code, body):
        self.status_code, self._body, self.text = status_code, body, ""

    def json(self):
        return self._body


class Platform:
    def __init__(self, monkeypatch, exposed, tags=({"id": 7, "name": "mcp"},)):
        self.exposed, self.tags, self.requested, self.predicted = exposed, list(tags), [], []
        monkeypatch.setattr(elitea.requests, "get", self.get)
        monkeypatch.setattr(elitea.requests, "post", self.post)

    def get(self, url, **kwargs):
        path = url.removeprefix(BASE)
        self.requested.append(path)
        if path.startswith("/tags/"):
            return Response(200, {"rows": self.tags})
        if path.startswith("/mcp_exposed_versions/"):
            return self.exposed
        if path.startswith("/applications/"):
            return Response(200, {"rows": [HELPER]})
        if path.startswith("/versions/"):
            return Response(200, [{"id": 10}, {"id": 11}])
        if path.startswith("/application/"):
            return Response(200, HELPER)
        if path.startswith("/version/"):
            return Response(200, VERSIONS[int(path.rsplit("/", 1)[1])])
        raise AssertionError(f"unexpected request {path}")

    def post(self, url, json=None, **kwargs):
        self.predicted.append((url.removeprefix(BASE), json))
        return Response(200, {"chat_history": [{"role": "assistant", "content": "done"}]})

    def agents(self):
        return Agents(base_url="http://platform.test", project_id=2, auth_token="token").agents


def served_tools(agents):
    handler = create_server(agents).request_handlers[types.ListToolsRequest]
    result = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    return {tool.name: tool for tool in result.root.tools}


def call(agents, name, arguments):
    handler = create_server(agents).request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(method="tools/call", params=types.CallToolRequestParams(name=name, arguments=arguments))
    return asyncio.run(handler(request)).root


def test_every_exposed_version_is_a_tool_under_the_platforms_name(monkeypatch):
    platform = Platform(monkeypatch, Response(200, {"rows": EXPOSED_ROWS}))
    tools = served_tools(platform.agents())
    assert sorted(tools) == ["Helper", "Helper_v2"]
    assert tools["Helper"].description == "does things"
    assert tools["Helper_v2"].description == "Version 'v2' of agent 'Helper'. does things"
    assert "tone" in tools["Helper_v2"].inputSchema["properties"]
    assert "tone" not in tools["Helper"].inputSchema["properties"]
    assert "/mcp_exposed_versions/prompt_lib/2" in platform.requested
    assert not any(path.startswith(("/applications/", "/versions/")) for path in platform.requested)


def test_a_call_runs_the_version_its_tool_names(monkeypatch):
    platform = Platform(monkeypatch, Response(200, {"rows": EXPOSED_ROWS}))
    agents = platform.agents()
    assert call(agents, "Helper_v2", {"user_input": "hi"}).isError is False
    assert call(agents, "Helper", {"user_input": "hi"}).isError is False
    assert [path for path, _ in platform.predicted] == ["/predict/prompt_lib/2/11", "/predict/prompt_lib/2/10"]


def test_only_a_non_default_version_tagged_serves_no_tool_under_the_agent_name(monkeypatch):
    platform = Platform(monkeypatch, Response(200, {"rows": EXPOSED_ROWS[1:]}))
    agents = platform.agents()
    assert sorted(served_tools(agents)) == ["Helper_v2"]
    assert call(agents, "Helper", {"user_input": "hi"}).isError is True
    assert platform.predicted == []


def test_no_exposed_version_serves_no_tool(monkeypatch):
    platform = Platform(monkeypatch, Response(200, {"rows": []}))
    assert platform.agents() == []


@pytest.mark.parametrize("unreported", [
    Response(404, {}),
    Response(200, {"rows": [{"application_id": 1, "application_name": "Helper", "version_id": 10,
                             "version_name": "base", "rule": "stored"}]}),
])
def test_a_platform_that_does_not_report_exposed_versions_gets_the_newest_version_as_before(
        monkeypatch, caplog, unreported):
    platform = Platform(monkeypatch, unreported)
    with caplog.at_level(logging.WARNING, logger=elitea.logger.name):
        agents = platform.agents()
    assert [(agent.tool_name, agent.version_id, agent.description) for agent in agents] == [("Helper", 11, "does things")]
    assert "does not report which agent versions" in caplog.text


def test_a_failing_exposed_versions_request_is_a_readable_error(monkeypatch):
    platform = Platform(monkeypatch, Response(503, {"error": "the versions could not be read"}))
    with pytest.raises(ApiDetailsRequestError, match="HTTP 503: the versions could not be read"):
        platform.agents()


def test_a_project_without_the_mcp_tag_keeps_its_readable_error(monkeypatch):
    platform = Platform(monkeypatch, Response(200, {"rows": []}), tags=())
    with pytest.raises(ApiDetailsRequestError, match='No agents tagged "mcp" in project 2'):
        platform.agents()


def test_a_pinned_agent_version_is_served_under_the_agent_name(monkeypatch):
    Platform(monkeypatch, Response(200, {"rows": []}))
    pinned = Agent(base_url="http://platform.test", project_id=2, auth_token="token", app_id=1, version_id=11)
    assert (pinned.tool_name, pinned.description) == ("Helper", "does things")
    assert sorted(served_tools(pinned)) == ["Helper"]
