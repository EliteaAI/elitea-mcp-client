from unittest.mock import MagicMock, patch

import pytest
import requests

from src.elitea_mcp.utils.preflight import (
    PreflightError,
    normalize_deployment_url,
    validate_platform_config,
    validate_project_id,
)

ORIGIN = "https://elitea.example.com"
SOCKET_URL = f"{ORIGIN}/socket.io/?EIO=4&transport=polling"
USER_URL = f"{ORIGIN}/api/v2/auth/user/"
PROJECT_URL = f"{ORIGIN}/api/v2/elitea_core/tags/prompt_lib/13"
CONFIG = {"deployment_url": ORIGIN, "auth_token": "token", "project_id": 13, "servers": {"github": {}}}


def make_response(status_code=200, content_type="application/json", location=None):
    """Build a requests-like response carrying only what the pre-flight reads."""
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"content-type": content_type}
    response.is_redirect = location is not None
    return response


def answer_platform(overrides=None):
    """Answer each pre-flight URL like a healthy platform unless an override is given for it."""
    responses = {
        SOCKET_URL: make_response(content_type="text/plain; charset=UTF-8"),
        USER_URL: make_response(),
        PROJECT_URL: make_response(),
    }
    responses.update(overrides or {})

    def get(url, **kwargs):
        outcome = responses[url]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return get


@pytest.mark.parametrize("raw_url, origin", [
    ("https://dev.elitea.ai/app/13/mcp", "https://dev.elitea.ai"),
    ("https://dev.elitea.ai/", "https://dev.elitea.ai"),
    ("http://localhost:8080", "http://localhost:8080"),
    (" https://dev.elitea.ai?x=1#top ", "https://dev.elitea.ai"),
])
def test_normalize_deployment_url_keeps_only_the_origin(raw_url, origin):
    assert normalize_deployment_url(raw_url) == origin


@pytest.mark.parametrize("raw_url", ["dev.elitea.ai", "", None, "ftp://dev.elitea.ai"])
def test_normalize_deployment_url_rejects_values_that_are_not_urls(raw_url):
    with pytest.raises(PreflightError, match="is not a valid URL"):
        normalize_deployment_url(raw_url)


def test_validate_project_id_accepts_digit_strings():
    assert validate_project_id("13") == 13


@pytest.mark.parametrize("value, message", [
    (None, '"project_id" is not set'),
    ("", '"project_id" is not set'),
    ("abc", '"project_id" must be a number, got "abc"'),
])
def test_validate_project_id_names_the_problem(value, message):
    with pytest.raises(PreflightError, match=message):
        validate_project_id(value)


@patch("src.elitea_mcp.utils.preflight.requests.get", side_effect=answer_platform())
def test_healthy_platform_returns_config_with_origin_and_integer_project(mock_get):
    notify = MagicMock()

    config = validate_platform_config({**CONFIG, "deployment_url": f"{ORIGIN}/app/13/mcp", "project_id": "13"}, notify)

    assert config["deployment_url"] == ORIGIN
    assert config["project_id"] == 13
    assert config["servers"] == {"github": {}}
    notify.assert_called_once_with(
        f'Deployment URL "{ORIGIN}/app/13/mcp" includes a page or endpoint path; connecting to the base address {ORIGIN} instead.'
    )
    assert mock_get.call_args_list[1].kwargs["headers"] == {"Authorization": "Bearer token"}


@patch("src.elitea_mcp.utils.preflight.requests.get", side_effect=answer_platform())
def test_base_url_with_trailing_slash_raises_no_notice(mock_get):
    notify = MagicMock()

    validate_platform_config({**CONFIG, "deployment_url": f"{ORIGIN}/"}, notify)

    notify.assert_not_called()


@pytest.mark.parametrize("overrides, message", [
    ({SOCKET_URL: requests.exceptions.ConnectionError("NameResolutionError: nodename nor servname provided")},
     f"Cannot reach {ORIGIN}: DNS lookup failed"),
    ({SOCKET_URL: requests.exceptions.ConnectionError("[Errno 61] Connection refused")},
     f"Cannot reach {ORIGIN}: connection refused or dropped"),
    ({SOCKET_URL: requests.exceptions.ConnectTimeout()},
     f"Cannot reach {ORIGIN}: the connection timed out"),
    ({SOCKET_URL: requests.exceptions.SSLError()},
     f"SSL certificate verification failed for {ORIGIN}"),
    ({SOCKET_URL: make_response(content_type="text/html")},
     f"{ORIGIN} responded, but it is not an ELITEA platform"),
    ({SOCKET_URL: make_response(status_code=404)},
     f"{ORIGIN} responded, but it is not an ELITEA platform"),
    ({USER_URL: make_response(status_code=401)},
     "Authentication failed: the token is invalid or expired"),
    ({USER_URL: make_response(status_code=302, location="/forward-auth/login")},
     "Authentication failed: the token is invalid or expired"),
    ({PROJECT_URL: make_response(status_code=403)},
     'Your token has no access to project 13, or the project does not exist'),
    ({PROJECT_URL: make_response(status_code=500)},
     "Could not verify access to project 13"),
])
def test_each_failure_gets_its_own_message(overrides, message):
    with patch("src.elitea_mcp.utils.preflight.requests.get", side_effect=answer_platform(overrides)):
        with pytest.raises(PreflightError, match=message):
            validate_platform_config(CONFIG, MagicMock())


@patch("src.elitea_mcp.utils.preflight.requests.get")
def test_missing_token_fails_before_any_authenticated_call(mock_get):
    mock_get.side_effect = answer_platform()

    with pytest.raises(PreflightError, match='"auth_token" is not set'):
        validate_platform_config({**CONFIG, "auth_token": ""}, MagicMock())

    assert [c.args[0] for c in mock_get.call_args_list] == [SOCKET_URL]


@patch("src.elitea_mcp.utils.preflight.requests.get")
def test_missing_project_fails_before_any_network_call(mock_get):
    with pytest.raises(PreflightError, match='"project_id" is not set'):
        validate_platform_config({**CONFIG, "project_id": None}, MagicMock())

    mock_get.assert_not_called()


@patch.dict("os.environ", {"ELITEA_DISABLE_SSL_VERIFY": "false"})
@patch("src.elitea_mcp.utils.preflight.requests.get", side_effect=answer_platform())
def test_ssl_verification_follows_the_socket_setting(mock_get):
    validate_platform_config(CONFIG, MagicMock())

    assert {c.kwargs["verify"] for c in mock_get.call_args_list} == {True}
