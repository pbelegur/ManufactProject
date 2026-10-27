"""One bounded MCP-check process; kill its owned forwarder on outer timeout."""
import os
import subprocess

from ui.jobs import stop_process


def run_check(command, timeout=200, cwd=None):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", shell=False, cwd=cwd, start_new_session=os.name != "nt")
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(process)
            process.communicate(timeout=15)
            raise RuntimeError("MCP check exceeded its total time limit; the check and its port-forward were stopped. No automatic retry was made.")
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process.poll() is None:
            stop_process(process)
