import json
from pathlib import Path


def test_both_servers_have_same_deployment_contract():
    root = Path(__file__).resolve().parents[1]
    for mode in ("stateless", "stateful"):
        folder = root / "servers" / mode
        cfg = json.loads((folder / "mcp.json").read_text())
        assert cfg["mode"] == mode
        assert cfg["replicas"] == 2
        assert cfg["port"] == 8000
        assert (folder / "Dockerfile").is_file()
        assert (folder / "server.py").is_file()
