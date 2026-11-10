"""Client release 2: on a protocol 2 platform, changes found during the session are pushed as a new snapshot."""
import time

import pytest

from src.elitea_mcp.utils.server_supervisor import ServerSupervisor
from src.elitea_mcp.utils.session_manager import get_session_manager
from tests import fixtures_7076 as fixtures
from tests.test_7076_platform_sync import Platform, legacy_ack, protocol_2_ack

QUIET_SECONDS = 1.5


@pytest.fixture(params=["protocol_2", "legacy"])
def platform(request):
    platform = Platform(protocol_2_ack if request.param == "protocol_2" else legacy_ack)
    platform.is_protocol_2 = request.param == "protocol_2"
    yield platform
    platform.stop()
    get_session_manager().cleanup_all()


def start(platform, servers):
    supervisor = ServerSupervisor(platform.config(servers))
    assert supervisor.start() is True
    supervisor.link.sio.reconnection = False
    return supervisor


def wait_for_payloads(platform, count):
    deadline = time.time() + 10
    while time.time() < deadline and len(platform.payloads) < count:
        time.sleep(0.05)
    time.sleep(QUIET_SECONDS if len(platform.payloads) < count else 0)
    return platform.payloads


def test_a_server_that_breaks_mid_session_is_taken_off_only_a_protocol_2_platform(platform, tmp_path):
    (tmp_path / "other").mkdir()
    ok, other = fixtures.healthy(tmp_path), fixtures.healthy(tmp_path / "other")
    supervisor = start(platform, {"ok": ok, "other": other})
    fixtures.break_server(ok)

    platform.call_tool("ok")
    platform.call_tool("ok")
    payloads = wait_for_payloads(platform, 2)

    if platform.is_protocol_2:
        assert [toolkit["name"] for toolkit in payloads[1]["toolkit_configs"]] == ["other"]
        assert [(item["name"], item["kind"]) for item in payloads[1]["failed_servers"]] == [("ok", "exited")]
    else:
        assert len(payloads) == 1
    platform.close(supervisor.link)


def test_a_stateful_server_announcing_new_tools_is_pushed_only_to_a_protocol_2_platform(platform, tmp_path):
    supervisor = start(platform, {"grows": fixtures.growing(tmp_path)})

    assert platform.call_tool("grows", tool="grow") == "grown"
    payloads = wait_for_payloads(platform, 2)

    if platform.is_protocol_2:
        assert [tool["name"] for tool in payloads[1]["toolkit_configs"][0]["tools"]] == ["grow", "added"]
        assert [tool["name"] for tool in supervisor.registry.get("grows").tools] == ["grow", "added"]
    else:
        assert len(payloads) == 1
        assert [tool["name"] for tool in supervisor.registry.get("grows").tools] == ["grow"]
    platform.close(supervisor.link)
