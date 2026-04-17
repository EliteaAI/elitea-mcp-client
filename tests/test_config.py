import json
import pytest
from pathlib import Path
from unittest.mock import patch, mock_open, MagicMock, call
from src.elitea_mcp.config import (
    load_config, save_config, set_bootstrap_config, get_bootstrap_config,
    get_config_dir, get_config_file, interactive_bootstrap, APP_NAME
)

@patch("src.elitea_mcp.config.open", new_callable=mock_open, read_data='{"key": "value"}')
@patch("src.elitea_mcp.config.get_config_file")
def test_load_config(mock_get_config_file, mock_open_file):
    class FakePath:
        def __init__(self, path):
            self._path = path
        def exists(self):
            return True
        def __str__(self):
            return self._path
    fake_path = FakePath("config.json")
    mock_get_config_file.return_value = fake_path
    config = load_config()
    assert config == {"key": "value"}

@patch("src.elitea_mcp.config.open", new_callable=mock_open)
@patch("src.elitea_mcp.config.get_config_file")
def test_save_config(mock_get_config_file, mock_open_file):
    mock_get_config_file.return_value = "config.json"
    save_config({"key": "value"})
    mock_open_file.assert_called_once_with("config.json", "w")
    written = "".join(call.args[0] for call in mock_open_file().write.call_args_list)
    assert written == '{\n  "key": "value"\n}'

@patch("src.elitea_mcp.config.load_config", return_value={})
@patch("src.elitea_mcp.config.save_config")
def test_set_bootstrap_config(mock_save_config, mock_load_config):
    config = set_bootstrap_config("http://example.com", "test_token")
    assert config["deployment_url"] == "http://example.com"
    assert config["auth_token"] == "test_token"
    mock_save_config.assert_called_once()

@pytest.fixture
def tmp_config_file(tmp_path):
    config = {"key": "value"}
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config))
    return config_file

def test_load_config_existing(monkeypatch, tmp_config_file):
    # Monkeypatch get_config_file to use our temporary config file
    monkeypatch.setattr("src.elitea_mcp.config.get_config_file", lambda: tmp_config_file)
    config = load_config()
    assert config["key"] == "value"

def test_load_config_missing(monkeypatch, tmp_path):
    # Monkeypatch get_config_file to point to a missing file
    missing_file = tmp_path / "nonexistent.json"
    monkeypatch.setattr("src.elitea_mcp.config.get_config_file", lambda: missing_file)
    config = load_config()
    # Expect an empty dict when the file is missing
    assert config == {}

def test_bootstrap_config(monkeypatch, tmp_path):
    # Monkeypatch get_config_file to use a temporary file for bootstrap config
    config_file = tmp_path / "config.json"
    monkeypatch.setattr("src.elitea_mcp.config.get_config_file", lambda: config_file)
    dep_url = "http://example.com"
    token = "abcd1234"
    host = "127.0.0.1"
    port = 8080
    set_bootstrap_config(dep_url, token, host, port)
    loaded = get_bootstrap_config()
    assert loaded.get("deployment_url") == dep_url
    assert loaded.get("auth_token") == token
    assert loaded.get("host") == host
    assert loaded.get("port") == port

def test_load_config_not_found(monkeypatch):
    # Monkeypatch get_config_file to return a path that does not exist.
    from pathlib import Path
    monkeypatch.setattr("src.elitea_mcp.config.get_config_file", lambda: Path("nonexistent_config.json"))
    config = load_config()
    assert config == {}

@patch("src.elitea_mcp.config.user_config_dir")
def test_get_config_dir(mock_user_config_dir):
    """Test that get_config_dir returns the correct directory and creates it if needed."""
    mock_dir = MagicMock()
    mock_dir_path = Path("/fake/config/dir")
    mock_user_config_dir.return_value = str(mock_dir_path)
    
    # Create a mock Path object that we can verify was called with mkdir
    mock_path = MagicMock(spec=Path)
    with patch("src.elitea_mcp.config.Path", return_value=mock_path) as mock_path_cls:
        result = get_config_dir()
        
        # Verify Path was called with the correct arguments
        mock_path_cls.assert_called_once_with(str(mock_dir_path))
        # Verify mkdir was called with the correct arguments
        mock_path.mkdir.assert_called_once_with(parents=True, exist_ok=True)
        # Verify the result is the mock path
        assert result == mock_path

def test_get_config_file():
    """Test that get_config_file returns the correct file path."""
    mock_dir = MagicMock()
    with patch("src.elitea_mcp.config.get_config_dir", return_value=mock_dir) as mock_get_config_dir:
        result = get_config_file()
        
        # Verify the result is constructed correctly
        assert result == mock_dir / "config.json"

@patch("src.elitea_mcp.config.input")
@patch("src.elitea_mcp.config.print")
@patch("src.elitea_mcp.config.set_bootstrap_config")
@patch("src.elitea_mcp.config.load_config")
def test_interactive_bootstrap_new_values(mock_load_config, mock_set_bootstrap, mock_print, mock_input):
    """Test interactive_bootstrap with new values provided."""
    mock_load_config.return_value = {}
    mock_input.side_effect = ["http://new.example.com", "new_token", "127.0.0.1", "9000"]
    mock_set_bootstrap.return_value = {"deployment_url": "http://new.example.com", "auth_token": "new_token", 
                                      "host": "127.0.0.1", "port": "9000"}
    
    result = interactive_bootstrap()
    
    # Verify set_bootstrap_config was called with correct arguments
    mock_set_bootstrap.assert_called_once_with("http://new.example.com", "new_token", "127.0.0.1", "9000")
    # Verify the result is what set_bootstrap_config returned
    assert result == {"deployment_url": "http://new.example.com", "auth_token": "new_token", 
                     "host": "127.0.0.1", "port": "9000"}

@patch("src.elitea_mcp.config.input")
@patch("src.elitea_mcp.config.print")
@patch("src.elitea_mcp.config.set_bootstrap_config")
@patch("src.elitea_mcp.config.load_config")
def test_interactive_bootstrap_keep_values(mock_load_config, mock_set_bootstrap, mock_print, mock_input):
    """Test interactive_bootstrap when keeping existing values."""
    mock_load_config.return_value = {"deployment_url": "http://old.example.com", "auth_token": "old_token", 
                                    "host": "0.0.0.0", "port": 8000}
    # Empty inputs to keep existing values
    mock_input.side_effect = ["", "", "", ""]
    
    result = interactive_bootstrap()
    
    # Verify set_bootstrap_config was called with the existing values
    mock_set_bootstrap.assert_called_once_with("http://old.example.com", "old_token", "0.0.0.0", 8000)
    # Verify the correct prints were made
    mock_print.assert_any_call("Current deployment URL: http://old.example.com")
    # Check for masked token
    mock_print.assert_any_call("Current auth token: ********oken")

@patch("src.elitea_mcp.config.input")
@patch("src.elitea_mcp.config.print")
@patch("src.elitea_mcp.config.set_bootstrap_config")
@patch("src.elitea_mcp.config.load_config")
def test_interactive_bootstrap_no_values(mock_load_config, mock_set_bootstrap, mock_print, mock_input):
    """Test interactive_bootstrap when no values are provided and none exist."""
    mock_load_config.return_value = {}
    # Empty inputs and no existing values
    mock_input.side_effect = ["", "", "", ""]
    
    result = interactive_bootstrap()
    
    # Verify set_bootstrap_config was not called
    mock_set_bootstrap.assert_not_called()
    # Verify the correct message was printed
    mock_print.assert_any_call("Configuration unchanged.")
    # Verify the result is the empty config
    assert result == {}

@patch("src.elitea_mcp.config.open", side_effect=IOError("Permission denied"))
@patch("src.elitea_mcp.config.get_config_file")
def test_save_config_io_error(mock_get_config_file, mock_open_file):
    """Test save_config handling of IO errors."""
    mock_get_config_file.return_value = "config.json"
    with pytest.raises(IOError, match="Permission denied"):
        save_config({"key": "value"})

@patch("src.elitea_mcp.config.open", new_callable=mock_open, read_data="invalid json")
@patch("src.elitea_mcp.config.json.load", side_effect=json.JSONDecodeError("Invalid JSON", "", 0))
@patch("src.elitea_mcp.config.get_config_file")
def test_load_config_json_error(mock_get_config_file, mock_json_load, mock_open_file):
    """Test load_config handling of invalid JSON."""
    # Set up a file that exists but has invalid JSON
    class FakePath:
        def exists(self):
            return True
        def __str__(self):
            return "config.json"
    mock_get_config_file.return_value = FakePath()
    
    # When JSON is invalid, json.load will raise JSONDecodeError
    # Let's verify that the function propagates this exception
    with pytest.raises(json.JSONDecodeError):
        load_config()
