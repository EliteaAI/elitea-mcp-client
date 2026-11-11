import logging
import sys
import threading
from pathlib import Path

import pytest

from src.elitea_mcp.utils import client_identity
from src.elitea_mcp.utils import logging_setup as src_logging_setup
from elitea_mcp.utils import logging_setup


@pytest.fixture(autouse=True)
def keep_client_id_out_of_the_real_config_dir(tmp_path, monkeypatch):
    """Every test that reaches the platform reads the client id; keep it in a temporary folder."""
    monkeypatch.setattr(client_identity, "get_config_dir", lambda: tmp_path)


@pytest.fixture(autouse=True)
def keep_logging_out_of_the_real_log_dir(tmp_path, monkeypatch):
    """A command under test sets up logging: keep its files in a temporary home and undo its handlers, levels, hooks and secrets."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    root_level = logging.getLogger().level
    yield
    for module in (logging_setup, src_logging_setup):
        module.remove_installed_handlers()
        module.reset_logger_levels()
        module.forget_log_secrets()
    logging.getLogger().setLevel(root_level)
    sys.excepthook = sys.__excepthook__
    threading.excepthook = threading.__excepthook__
