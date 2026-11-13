import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from src.elitea_mcp import config as config_module
from src.elitea_mcp import config_errors as config_errors_module
from src.elitea_mcp import main as main_module
from src.elitea_mcp import tray as tray_module
from src.elitea_mcp.config import load_config, save_config, set_bootstrap_config
from src.elitea_mcp.config_errors import ConfigError, ConfigMistake, locate_json_mistake

VALID_CONFIG = {"deployment_url": "https://elitea.example.com", "auth_token": "token", "servers": {"fs": {"command": "npx"}}}


@pytest.fixture
def config_file(monkeypatch, tmp_path):
    """Point the client at a config file in a temporary folder."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "get_config_file", lambda: path)
    monkeypatch.setattr(tray_module, "get_config_file", lambda: path)
    monkeypatch.setattr(main_module, "get_config_file", lambda: path)
    return path


@pytest.mark.parametrize("text, line, column, mistake", [
    ('{\n  "command": "C:\\Users\\me\\node.exe"\n}', 2, 17, ConfigMistake.BACKSLASH),
    ('{\n  "a": 1,\n}', 2, 9, ConfigMistake.TRAILING_COMMA),
    ('{\n  "a": [1, 2,]\n}', 2, 13, ConfigMistake.TRAILING_COMMA),
    ('{\n  // note\n  "a": 1\n}', 2, 3, ConfigMistake.COMMENT),
    ('{\n  "a": 1 // note\n}', 2, 10, ConfigMistake.COMMENT),
    ('{\n  "a": 1,\n  # note\n  "b": 2\n}', 3, 3, ConfigMistake.COMMENT),
    ("{\n  'a': 1\n}", 2, 3, ConfigMistake.SINGLE_QUOTE),
    ("{\n  \"a\": 'x'\n}", 2, 8, ConfigMistake.SINGLE_QUOTE),
    ('{\n  "a": 1\n  "b": 2\n}', 3, 3, ConfigMistake.MISSING_COMMA),
    ('{\n  "a": 1\n', 3, 1, ConfigMistake.UNCLOSED),
    ("", 1, 1, ConfigMistake.EMPTY_FILE),
    ("  \n", 1, 1, ConfigMistake.EMPTY_FILE),
])
def test_each_common_mistake_is_reported_with_its_position_and_hint(config_file, text, line, column, mistake):
    config_file.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert (caught.value.line, caught.value.column, caught.value.mistake) == (line, column, mistake)
    assert f"line {line}, column {column}: " in str(caught.value)
    assert f"Hint: {mistake.hint}" in str(caught.value)


def test_the_python_3_13_trailing_comma_error_gets_the_trailing_comma_hint():
    error = json.JSONDecodeError("Illegal trailing comma before end of object", '{"a": 1,}', 7)
    assert locate_json_mistake(error) == (7, "Illegal trailing comma before end of object", ConfigMistake.TRAILING_COMMA)


def test_the_message_shows_the_file_the_offending_line_and_a_caret_under_the_mistake(config_file):
    config_file.write_text('{\n\t"cwd": "C:\\Users\\me\\project"\n}', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert str(caught.value) == (
        f"Config file is not valid JSON: {config_file}\n"
        "  line 2, column 12: Invalid \\escape\n"
        '    \t"cwd": "C:\\Users\\me\\project"\n'
        "    \t          ^\n"
        f"Hint: {ConfigMistake.BACKSLASH.hint}"
    )


def test_a_file_that_is_not_a_json_object_is_rejected_with_a_hint(config_file):
    config_file.write_text('["servers"]', encoding="utf-8")
    with pytest.raises(ConfigError, match="It holds a JSON list instead") as caught:
        load_config()
    assert caught.value.mistake is ConfigMistake.NOT_OBJECT


def test_a_file_in_another_encoding_is_reported_instead_of_raising_a_decode_error(config_file):
    config_file.write_bytes('{"deployment_url": "https://café.example.com"}'.encode("cp1252"))
    with pytest.raises(ConfigError, match="not UTF-8 text"):
        load_config()


def test_a_config_saved_with_a_utf8_bom_loads(config_file):
    config_file.write_bytes(b"\xef\xbb\xbf" + json.dumps({"deployment_url": "https://café.example.com"}).encode())
    assert load_config()["deployment_url"] == "https://café.example.com"


def test_a_failed_save_leaves_the_previous_file_untouched_and_no_temp_file(config_file):
    save_config(VALID_CONFIG)
    with pytest.raises(TypeError):
        save_config({"deployment_url": "https://elitea.example.com", "servers": object()})
    assert json.loads(config_file.read_text()) == VALID_CONFIG
    assert sorted(path.name for path in config_file.parent.iterdir()) == ["config.json", "config.json.bak"]


def test_saving_keeps_the_replaced_valid_config_as_the_backup(config_file):
    save_config(VALID_CONFIG)
    save_config({**VALID_CONFIG, "port": 9000})
    assert json.loads((config_file.parent / "config.json.bak").read_text()) == VALID_CONFIG


def test_saving_over_a_broken_file_keeps_the_last_valid_backup(config_file):
    save_config(VALID_CONFIG)
    save_config({**VALID_CONFIG, "port": 9000})
    config_file.write_text('{"port": 9000,}', encoding="utf-8")
    save_config({**VALID_CONFIG, "port": 9001})
    assert json.loads((config_file.parent / "config.json.bak").read_text()) == VALID_CONFIG


def test_the_error_points_to_the_backup_when_one_exists(config_file):
    save_config(VALID_CONFIG)
    save_config(VALID_CONFIG)
    config_file.write_text("{", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert str(caught.value).endswith(f"The last valid config is saved in {config_file}.bak")


def test_bootstrap_refuses_to_overwrite_a_file_with_a_syntax_error(config_file):
    config_file.write_text('{"servers": {"fs": {}},}', encoding="utf-8")
    with pytest.raises(ConfigError):
        set_bootstrap_config("https://elitea.example.com", "token")
    assert config_file.read_text() == '{"servers": {"fs": {}},}'


def run_cli(monkeypatch, *args):
    """Run the elitea-mcp command line and return its exit code."""
    monkeypatch.setattr(sys, "argv", ["elitea-mcp", *args])
    try:
        main_module.cli()
    except SystemExit as exit_:
        return exit_.code
    return 0


def test_validate_exits_zero_for_a_valid_config(config_file, monkeypatch, capsys):
    save_config(VALID_CONFIG)
    assert run_cli(monkeypatch, "validate") == 0
    assert capsys.readouterr().out == f"Config file is valid: {config_file}\n"


def test_validate_exits_non_zero_and_prints_the_error_for_a_broken_config(tmp_path, monkeypatch, capsys):
    broken = tmp_path / "other.json"
    broken.write_text('{"a": 1,}', encoding="utf-8")
    assert run_cli(monkeypatch, "validate", "--config", str(broken)) == 1
    assert capsys.readouterr().err.startswith(f"Config file is not valid JSON: {broken}\n  line 1, column 8: ")


def test_validate_exits_non_zero_for_a_missing_config(tmp_path, monkeypatch, capsys):
    assert run_cli(monkeypatch, "validate", "--config", str(tmp_path / "missing.json")) == 1
    assert "Config file not found" in capsys.readouterr().err


def test_other_commands_print_the_readable_error_instead_of_a_traceback(config_file, monkeypatch, capsys):
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    monkeypatch.setattr(main_module, "run_servers", lambda *args: load_config())
    assert run_cli(monkeypatch, "serve") == 1
    assert "Hint: Remove the comma after the last item." in capsys.readouterr().err


def build_tray():
    """Create a tray app on the temporary config without real servers or update checks."""
    with patch.object(tray_module, "get_server_controller", return_value=MagicMock(daemon_mode=False)), \
            patch.object(tray_module, "UpdateChecker"):
        app = tray_module.MCPTrayApp()
    app.show_notification = MagicMock()
    return app


def menu_texts(app):
    return [item.text for item in app.get_menu_items()]


def test_the_tray_starts_on_a_broken_config_with_a_usable_menu(config_file):
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    app = build_tray()
    texts = menu_texts(app)
    assert "Config File Has Errors" in texts
    assert "Start MCP Server" not in texts
    assert "Quit" in texts
    configuration = next(item for item in app.get_menu_items() if item.text == "Configuration")
    assert [entry.text for entry in configuration.submenu.items][:2] == ["Open Config File", "Validate Config"]


def test_the_tray_announces_a_config_error_once_when_it_appears(config_file):
    save_config(VALID_CONFIG)
    app = build_tray()
    menu_texts(app)
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    menu_texts(app)
    menu_texts(app)
    app.show_notification.assert_called_once_with(
        "Invalid Config", f"line 1, column 8: Trailing comma before }}. {ConfigMistake.TRAILING_COMMA.hint}")


def test_the_tray_reports_a_broken_config_at_startup_once_the_icon_is_up(config_file):
    config_file.write_text("", encoding="utf-8")
    app = build_tray()
    app.report_update_result = MagicMock()
    app._on_tray_ready(MagicMock())
    app.show_notification.assert_called_once_with("Invalid Config", f"line 1, column 1: The file is empty. {ConfigMistake.EMPTY_FILE.hint}")


def test_validate_config_brings_back_start_once_the_file_is_fixed(config_file):
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    app = build_tray()
    app.icon = MagicMock()
    config_file.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
    app.validate_config()
    app.show_notification.assert_called_once_with("Config Valid", f"{config_file} is valid.")
    assert "Start MCP Server" in [item.text for item in app.icon.menu.items]


def test_validate_config_repeats_an_unchanged_error(config_file):
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    app = build_tray()
    menu_texts(app)
    app.validate_config()
    app.show_notification.assert_called_once()
    assert app.show_notification.call_args.args[0] == "Invalid Config"


@pytest.mark.parametrize("action", ["start_server", "restart_server", "view_config"])
def test_server_and_config_actions_report_a_broken_config_without_raising(config_file, action):
    save_config(VALID_CONFIG)
    app = build_tray()
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    getattr(app, action)()
    app.server_controller.start_servers.assert_not_called()
    app.server_controller.restart_servers.assert_not_called()
    assert app.show_notification.call_args.args[0] == "Invalid Config"


def test_a_running_server_can_still_be_stopped_while_the_config_is_broken(config_file):
    save_config(VALID_CONFIG)
    app = build_tray()
    app.is_server_running = True
    config_file.write_text('{"a": 1,}', encoding="utf-8")
    texts = menu_texts(app)
    assert "Stop MCP Server" in texts
    assert "Restart MCP Server" not in texts


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS notifications need AppKit")
def test_macos_notifications_escape_backslashes_and_quotes_for_applescript():
    from src.elitea_mcp.tray_platforms import macos
    with patch.object(macos.subprocess, "run") as run:
        macos.MacOSTray().notify(None, "Invalid Config", f"Expecting ',' delimiter. {ConfigMistake.BACKSLASH.hint}")
    script = run.call_args.args[0][2]
    assert ('display notification "Expecting \',\' delimiter. Backslashes in Windows paths must be doubled '
            '(\\"C:\\\\\\\\Users\\\\\\\\...\\") or use forward slashes (\\"C:/Users/...\\")."') in script


@pytest.mark.parametrize("line", [
    '  "auth_token": "SECRET_TOKEN_123",',
    '  "headers": {"Authorization": "Bearer SECRET_TOKEN_123"},',
    '  "password": "SECRET_TOKEN_123",',
    '  "auth_token": "SECRET_TOKEN_123',
    "  'auth_token': 'SECRET_TOKEN_123',",
    "  \"auth_token\": 'SECRET_TOKEN_123',",
    '  "auth_token": SECRET_TOKEN_123,',
    '  "auth_token": "SECRET_[TOKEN_123',
])
def test_the_error_excerpt_never_shows_a_secret_value(config_file, line):
    config_file.write_text("{\n  \"deployment_url\": \"https://elitea.example.com\"\n" + line + "\n}", encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert caught.value.line == 3
    excerpt = str(caught.value).splitlines()[2]
    assert len(excerpt) == len(f"    {line}")
    assert "SECRET_" not in str(caught.value)
    assert "TOKEN_123" not in str(caught.value)


def test_a_broken_config_at_launch_never_starts_background_update_checks(config_file):
    config_file.write_text('{"update_check": false,}', encoding="utf-8")
    with patch.object(tray_module, "get_server_controller", return_value=MagicMock(daemon_mode=False)), \
            patch.object(tray_module, "UpdateChecker") as checker:
        app = tray_module.MCPTrayApp()
    checker.assert_not_called()
    assert app.update_checker is None


def test_redaction_leaves_lines_without_secret_keys_untouched():
    line = '  "host": "0.0.0.0", "cwd": "C:\\Users\\me\\project", "stateful": true'
    assert config_errors_module.redact_values(line) == line


def test_redaction_masks_every_secret_on_a_one_line_config():
    line = '{"auth_token": "SECRET_ONE", "headers": {"Authorization": "Bearer \\"SECRET_TWO"}, "port": 1,}'
    redacted = config_errors_module.redact_values(line)
    assert "SECRET" not in redacted
    assert len(redacted) == len(line)
    assert '"port": 1,}' in redacted


def test_a_server_whose_name_holds_a_secret_word_keeps_its_windows_path_excerpt(config_file):
    line = '  "azure-keyvault": {"cwd": "C:\\Program Files\\nodejs", "type": "stdio"}'
    config_file.write_text('{\n  "servers": {\n' + line + '\n  }\n}', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config()
    assert caught.value.mistake is ConfigMistake.BACKSLASH
    assert str(caught.value).splitlines()[2] == f"    {line}"


@pytest.mark.parametrize("line", [
    '{"command": "npx srv --api-key=SECRET_ONE"}',
    '{"args": ["--header", "Authorization: Bearer SECRET_ONE"]}',
    '{"args": [\n  "--token",\n  "SECRET_ONE"\n]}',
])
def test_command_lines_are_masked_because_they_often_carry_a_token(line):
    redacted = config_errors_module.redact_values(line)
    assert "SECRET_ONE" not in redacted
    assert len(redacted) == len(line)


def test_a_value_under_a_key_the_client_does_not_know_is_masked(config_file):
    line = '  "fs": {"type": "stdio", "env": {"GITHUB_PAT": "ghp_SECRET", "PATH": "/bin",}}'
    config_file.write_text('{\n  "servers": {\n' + line + '\n  }\n}', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        load_config()
    excerpt = str(caught.value).splitlines()[2]
    assert "ghp_SECRET" not in excerpt
    assert '"type": "stdio"' in excerpt


def test_an_array_element_on_its_own_line_is_masked_by_the_key_that_owns_the_array():
    text = '{"tokens": [\n  "SECRET_A",\n  ["SECRET_B"]\n], "port": [\n  8000\n]}'
    redacted = config_errors_module.redact_values(text)
    assert "SECRET" not in redacted
    assert "8000" in redacted
    assert len(redacted) == len(text)


def test_a_server_url_is_masked_because_it_can_carry_an_api_key():
    line = '{"type": "sse", "url": "https://mcp.example.com/sse?api_key=SECRET"}'
    assert "SECRET" not in config_errors_module.redact_values(line)


@pytest.mark.parametrize("line", [
    '{"env": {"DATABASE_URL": postgres://admin:SECRET_PW@db.internal:5432/app}}',
    '{"api_token": redis://:SECRET_PW@cache:6379/0}',
    '{"url": https://svc:SECRET_PW@mcp.example.com:8443/sse}',
])
def test_an_unquoted_value_with_colons_is_fully_masked(line):
    redacted = config_errors_module.redact_values(line)
    assert "SECRET_PW" not in redacted
    assert len(redacted) == len(line)


def test_an_unquoted_key_still_shows():
    assert config_errors_module.redact_values('{"a": 1, port: 8000}') == '{"a": *, port: 8000}'
