import pytest

from src.elitea_mcp.utils import client_identity


@pytest.fixture(autouse=True)
def keep_client_id_out_of_the_real_config_dir(tmp_path, monkeypatch):
    """Every test that reaches the platform reads the client id; keep it in a temporary folder."""
    monkeypatch.setattr(client_identity, "get_config_dir", lambda: tmp_path)
