import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts import ui_mcp_check as helper
from ui.checks import run_check


def test_forwarding_parser_ignores_error_urls_and_accepts_service_target_mapping():
    assert helper.forwarded_port("error connecting https://127.0.0.1:4567") is None
    assert helper.forwarded_port("Forwarding from 127.0.0.1:4567 -> 8080\n") == 4567
    assert helper.forwarded_port("Forwarding from 127.0.0.1:4567 -> 8000\n") == 4567
    assert helper.forwarded_port("Forwarding from 127.0.0.1:99999 -> 8000\n") is None


def test_check_uses_gateway_host_and_calls_tool_once_then_cleans_forwarder(monkeypatch):
    calls, processes = [], []
    class Process:
        pid = 12345
        def __init__(self, command, **kwargs):
            processes.append(command)
            kwargs["stdout"].write("Forwarding from 127.0.0.1:1234 -> 8080\n")
            kwargs["stdout"].flush()
        def poll(self):
            return None
        def wait(self, timeout):
            return 0
        def terminate(self):
            calls.append("terminate")
    monkeypatch.setattr(helper.subprocess, "Popen", Process)
    monkeypatch.setattr(helper.subprocess, "run", lambda command, **kwargs: calls.append(command))
    async def check(args, url, host):
        calls.append((url, host, args.tool))
        return {"success": True, "attempts": 1}
    monkeypatch.setattr(helper, "check", check)
    args = SimpleNamespace(tool="get_counter", namespace="mcp-auto", name="counter", backend="knative", kubeconfig="fixture", forward_timeout=1)
    result = helper.main(args)
    assert result["success"] and result["attempts"] == 1 and result["forward_setup_ms"] >= 0
    assert calls[0] == ("http://127.0.0.1:1234", "counter.mcp-auto.mcp.test", "get_counter")
    assert "kourier-system" in processes[0] and "svc/kourier" in processes[0]
    assert len(calls) == 2  # One tool invocation and one owned-process cleanup.


def test_outer_timeout_stops_owned_process_without_retry(tmp_path):
    with pytest.raises(RuntimeError, match="No automatic retry"):
        run_check([sys.executable, "-u", "-c", "import time;time.sleep(10)"], timeout=.1, cwd=tmp_path)
