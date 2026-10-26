"""One-process bounded queue and job-local subprocess/log handling.

No Docker/Kubernetes deployment logic lives here. The executor runs deployctl.
"""
import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import threading

from ui.monitoring import redact

STAGES = ("QUEUED", "BUILDING", "PUSHING", "DEPLOYING", "VERIFYING", "SUCCEEDED", "FAILED")
EVENT_PREFIX = "@@MCP_DEPLOY_STAGE "


def now():
    return datetime.now(timezone.utc).isoformat()


def worker_limit(value=None):
    value = os.environ.get("MCP_UI_MAX_JOBS", "2") if value is None else value
    if str(value) not in {"1", "2", "3"}:
        raise ValueError("MCP_UI_MAX_JOBS must be 1, 2 or 3 (default 2)")
    return int(value)


def snapshot_project(source, destination):
    """Copy only a contained project; never copy links or local credentials."""
    source, destination = Path(source).resolve(), Path(destination)
    omitted = {".git", ".local", ".aws", ".kube", ".ssh", ".venv", ".venv-ui", "__pycache__", "node_modules", ".pytest_cache"}
    destination.mkdir(parents=True, exist_ok=False)
    total = count = 0
    for parent, directories, filenames in os.walk(source, followlinks=False):
        directories[:] = [d for d in directories if d not in omitted]
        for name in directories + filenames:
            path = Path(parent) / name
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise ValueError("Build source contains a symlink or junction")
        for name in filenames:
            if name == ".env" or (name.startswith(".env.") and name != ".env.example") or name.endswith((".pem", ".key", ".kubeconfig")):
                continue
            path = Path(parent) / name
            if not path.resolve().is_relative_to(source):
                raise ValueError("Build source escaped the registered project")
            count += 1
            total += path.stat().st_size
            if count > 2000 or total > 50 * 1024 * 1024:
                raise ValueError("Build snapshot exceeds 2,000 files or 50 MiB")
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    return {"files": count, "bytes": total, "at": now()}


class DeploymentQueue:
    """Oldest eligible work first; conflicting keys retain submission order."""
    def __init__(self, concurrency=2, capacity=8):
        self.concurrency = worker_limit(concurrency)
        self.capacity = capacity
        self.pending = deque()
        self.active = {}
        self.condition = asyncio.Condition()
        self.closing = False

    async def put(self, job, keys):
        async with self.condition:
            if self.closing:
                raise RuntimeError("Deployment queue is closing")
            if len(self.pending) >= self.capacity:
                raise asyncio.QueueFull
            self.pending.append((job, frozenset(keys)))
            self.condition.notify_all()

    async def claim(self):
        async with self.condition:
            while True:
                if self.closing and not self.pending:
                    return None
                occupied = set().union(*self.active.values()) if self.active else set()
                earlier = set()
                if len(self.active) < self.concurrency:
                    for index, (job, keys) in enumerate(self.pending):
                        if not keys.intersection(occupied | earlier):
                            del self.pending[index]
                            self.active[job["id"]] = keys
                            return job
                        earlier.update(keys)
                await self.condition.wait()

    async def release(self, job_id):
        async with self.condition:
            self.active.pop(job_id, None)
            self.condition.notify_all()

    async def close(self):
        async with self.condition:
            self.closing = True
            self.condition.notify_all()

    def waiting(self, job_id):
        occupied = set().union(*self.active.values()) if self.active else set()
        earlier = set()
        for index, (job, keys) in enumerate(self.pending):
            if job["id"] == job_id:
                conflicts = sorted(keys.intersection(occupied | earlier))
                return {"queue_position": index + 1,
                        "waiting_reason": "Waiting for resource: " + ", ".join(conflicts) if conflicts else "Waiting for a worker slot"}
            earlier.update(keys)
        return {"queue_position": None, "waiting_reason": None}


class LogRedactor:
    def __init__(self, values=()):
        from urllib.parse import quote
        self.values = sorted({variant for value in values if isinstance(value, str) and len(value) >= 6
                              for variant in (value, quote(value, safe=""))}, key=len, reverse=True)
        self.private_key = False

    def clean(self, text):
        if "-----BEGIN " in text and any(x in text for x in ("PRIVATE KEY", "CERTIFICATE")):
            self.private_key = True
            return "[redacted credential block]"
        if self.private_key:
            if "-----END " in text:
                self.private_key = False
            return None
        for value in self.values:
            text = text.replace(value, "[redacted]")
        return redact(text)


class BoundedLog:
    """Three bounded UTF-8 segments; only redacted output ever reaches disk."""
    def __init__(self, path, max_bytes=1024 * 1024, guard=None, redactor=None):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        if not 65536 <= self.max_bytes <= 16 * 1024 * 1024:
            raise ValueError("MCP_UI_JOB_LOG_BYTES must be 65536..16777216 per segment")
        self.guard = guard or threading.RLock()
        self.redactor = redactor or LogRedactor()
        self.rotations = 0

    def append(self, text):
        clean = self.redactor.clean(text.rstrip("\r\n"))
        if clean is None:
            return
        encoded = (now() + " " + clean + "\n").encode("utf-8", errors="replace")
        if len(encoded) > self.max_bytes:
            encoded = encoded[:self.max_bytes - 40] + b" [entry truncated after redaction]\n"
        with self.guard:
            if self.path.exists() and self.path.stat().st_size + len(encoded) > self.max_bytes:
                oldest = self.path.with_name(self.path.name + ".2")
                oldest.unlink(missing_ok=True)
                previous = self.path.with_name(self.path.name + ".1")
                if previous.exists():
                    previous.replace(oldest)
                self.path.replace(previous)
                self.rotations += 1
            with self.path.open("ab") as target:
                target.write(encoded)

    def tail(self, limit=256_000):
        with self.guard:
            chunks, remaining = [], limit
            for path in [self.path, self.path.with_name(self.path.name + ".1"), self.path.with_name(self.path.name + ".2")]:
                if not path.exists() or remaining <= 0:
                    continue
                with path.open("rb") as source:
                    size = source.seek(0, 2)
                    source.seek(max(0, size - remaining))
                    chunk = source.read(remaining)
                remaining -= len(chunk)
                chunks.insert(0, chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")


def stop_process(process):
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15)
    else:
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=15)


def run_pipeline(command, *, cwd, env, log, on_stage, on_result, on_pid, timeout=1200):
    """Drain stdout/stderr throughout execution; bounded lines and timeout."""
    process = subprocess.Popen(command, cwd=cwd, env=env, shell=False, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, bufsize=0, start_new_session=os.name != "nt")
    on_pid(process.pid)
    errors = []

    def pump():
        dropping = False
        try:
            while raw := process.stdout.readline(16385):
                if len(raw) > 16384 and not raw.endswith(b"\n"):
                    if not dropping:
                        log.append("[Overlong output line omitted]")
                    dropping = True
                    continue
                if dropping:
                    dropping = not raw.endswith(b"\n")
                    continue
                line = raw.decode("utf-8", errors="replace")
                log.append(line)
                if line.startswith(EVENT_PREFIX):
                    try:
                        event = json.loads(line[len(EVENT_PREFIX):])
                        if event.get("stage") in STAGES[1:-2] and event.get("job_id") == env.get("MCP_DEPLOY_JOB_ID"):
                            on_stage(event["stage"], event.get("message", ""))
                    except (ValueError, TypeError):
                        pass
                elif line.startswith("{"):
                    try:
                        result = json.loads(line)
                        if isinstance(result, dict) and result.get("status") == "healthy":
                            on_result(result)
                    except ValueError:
                        pass
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=pump, name="job-output-" + env.get("MCP_DEPLOY_JOB_ID", "test"), daemon=True)
    reader.start()
    try:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(process)
            raise RuntimeError("Deployment exceeded its execution timeout; inspect Kubernetes before retrying")
        reader.join(timeout=15)
        if reader.is_alive():
            raise RuntimeError("CLI output pipe did not close; inspect remaining child processes")
        if errors:
            raise RuntimeError("Cannot retain deployment output: " + str(errors[0]))
        return code
    finally:
        if process.poll() is None:
            stop_process(process)
        if not reader.is_alive():
            process.stdout.close()
