import asyncio
import json
import os
from pathlib import Path
import re
import sys
import threading

import pytest

from ui.jobs import BoundedLog, DeploymentQueue, LogRedactor, run_pipeline, snapshot_project, worker_limit


@pytest.mark.parametrize("value", [0, 4, -1, "abc", "3.5", True])
def test_worker_limit_rejects_unbounded_or_invalid_values(value):
    with pytest.raises(ValueError):
        worker_limit(value)


def test_worker_default_and_lower_configuration(monkeypatch):
    monkeypatch.delenv("MCP_UI_MAX_JOBS", raising=False)
    assert worker_limit() == 2
    for value in (1, 2, 3):
        monkeypatch.setenv("MCP_UI_MAX_JOBS", str(value))
        assert worker_limit() == value


def test_queue_conflict_fifo_no_head_of_line_block_and_shutdown():
    async def scenario():
        queue = DeploymentQueue(3, capacity=3)
        await queue.put({"id": "first"}, ["ns/alpha"])
        assert (await queue.claim())["id"] == "first"
        await queue.put({"id": "conflict"}, ["ns/alpha"])
        await queue.put({"id": "independent"}, ["ns/beta"])
        assert "ns/alpha" in queue.waiting("conflict")["waiting_reason"]
        assert (await queue.claim())["id"] == "independent"
        await queue.put({"id": "later"}, ["ns/alpha"])
        await queue.close()
        waiting = asyncio.create_task(queue.claim())
        await asyncio.sleep(.01)
        assert not waiting.done()
        await queue.release("first")
        assert (await waiting)["id"] == "conflict"
        await queue.release("conflict")
        assert (await queue.claim())["id"] == "later"
        await queue.release("later")
        await queue.release("independent")
        assert await queue.claim() is None
    asyncio.run(scenario())


def test_queue_capacity_and_active_cap():
    async def scenario():
        queue = DeploymentQueue(1, capacity=1)
        await queue.put({"id": "a"}, ["a"])
        await queue.claim()
        await queue.put({"id": "b"}, ["b"])
        with pytest.raises(asyncio.QueueFull):
            await queue.put({"id": "c"}, ["c"])
        claim = asyncio.create_task(queue.claim())
        await asyncio.sleep(.01)
        assert not claim.done() and len(queue.active) == 1
        await queue.release("a")
        assert (await claim)["id"] == "b"
        await queue.release("b")
        await queue.close()
    asyncio.run(scenario())


def test_multi_resource_lock_does_not_overtake_blocked_predecessor():
    async def scenario():
        queue = DeploymentQueue(3)
        await queue.put({"id": "redis-owner"}, ["redis"])
        await queue.claim()
        await queue.put({"id": "first-app"}, ["app", "redis"])
        await queue.put({"id": "second-app"}, ["app"])
        await queue.put({"id": "other"}, ["other"])
        assert (await queue.claim())["id"] == "other"
        await queue.release("redis-owner")
        assert (await queue.claim())["id"] == "first-app"
        await queue.release("first-app")
        assert (await queue.claim())["id"] == "second-app"
        await queue.release("second-app")
        await queue.release("other")
        await queue.close()
    asyncio.run(scenario())


def test_logs_redacted_before_disk_bounded_rotated_and_isolated(tmp_path):
    log = BoundedLog(tmp_path / "first.log", 65536, redactor=LogRedactor(["unlabelled-super-secret"]))
    for text in ["unlabelled-super-secret", 'Authorization: Bearer abcDEF123', '{"password":"with spaces"}',
                 "redis://user:private@host/0", "AWS_SECRET_ACCESS_KEY=actual-key", "-----BEGIN PRIVATE KEY-----",
                 "encoded-private-data", "-----END PRIVATE KEY-----", "after-key"]:
        log.append(text)
    raw = log.path.read_text()
    for secret in ["unlabelled-super-secret", "abcDEF123", "with spaces", "user:private", "actual-key", "encoded-private-data"]:
        assert secret not in raw
    assert "after-key" in raw
    assert re.search(r"\d{4}-\d\d-\d\dT", raw)
    for number in range(100):
        log.append(f"line {number} " + "x" * 4000)
    files = list(tmp_path.glob("first.log*"))
    assert len(files) == 3 and all(f.stat().st_size <= 65536 for f in files)
    other = BoundedLog(tmp_path / "second.log", 65536)
    other.append("only-other-job")
    assert "only-other-job" not in log.tail()
    assert len(log.tail(1000).encode()) <= 1000
    assert "line 99" in log.tail()
    log.append("z" * 100000)
    assert all(f.stat().st_size <= 65536 for f in tmp_path.glob("first.log*"))


def test_pipeline_streams_before_completion_and_only_accepts_own_stages(tmp_path):
    identifier = "a" * 32
    log = BoundedLog(tmp_path / "stream.log", 65536)
    seen = threading.Event()
    stages, results = [], []
    script = "import json,time; print('@@MCP_DEPLOY_STAGE '+json.dumps({'stage':'BUILDING','job_id':'" + identifier + "','message':'start'}),flush=True); print('@@MCP_DEPLOY_STAGE '+json.dumps({'stage':'VERIFYING','job_id':'other'}),flush=True); print('@@MCP_DEPLOY_STAGE '+json.dumps({'stage':'SUCCEEDED','job_id':'" + identifier + "'}),flush=True); time.sleep(.2); print('{\"status\":\"healthy\"}',flush=True)"
    def stage(name, message):
        assert "BUILDING" in log.tail()
        assert not results, "The stage must arrive before the final CLI result"
        seen.set()
        stages.append(name)
    code = run_pipeline([sys.executable, "-u", "-c", script], cwd=tmp_path,
        env={**os.environ, "MCP_DEPLOY_JOB_ID": identifier}, log=log, on_stage=stage, on_result=results.append, on_pid=lambda _: None, timeout=5)
    assert code == 0 and seen.is_set() and stages == ["BUILDING"] and results[0]["status"] == "healthy"


def test_overlong_output_and_timeout_do_not_fill_disk(tmp_path):
    log = BoundedLog(tmp_path / "large.log", 65536)
    kwargs = {"cwd": tmp_path, "env": os.environ.copy(), "log": log, "on_stage": lambda *_: None,
              "on_result": lambda _: None, "on_pid": lambda _: None}
    assert run_pipeline([sys.executable, "-u", "-c", "print('x'*100000);print('finished')"], **kwargs, timeout=5) == 0
    assert log.path.stat().st_size < 1000 and "finished" in log.tail()
    with pytest.raises(RuntimeError, match="timeout"):
        run_pipeline([sys.executable, "-u", "-c", "import time;time.sleep(10)"], **kwargs, timeout=.2)


def test_snapshots_are_private_and_exclude_local_credentials(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "server.py").write_text("original")
    (source / ".env").write_text("SECRET=hidden")
    (source / ".aws").mkdir()
    (source / ".aws/credentials").write_text("hidden")
    first, second = tmp_path / "one", tmp_path / "two"
    snapshot_project(source, first)
    snapshot_project(source, second)
    (first / "server.py").write_text("changed")
    assert (second / "server.py").read_text() == (source / "server.py").read_text() == "original"
    assert not (first / ".env").exists() and not (first / ".aws").exists()


def test_real_platform_workers_parallelism_failure_isolation_and_conflicts(tmp_path, monkeypatch):
    # The actual Platform and subprocess runner, with a tiny CLI fixture instead
    # of Docker/Kubernetes. ROOT is isolated BEFORE Platform is constructed.
    import ui.app as application
    monkeypatch.setattr(application, "ROOT", tmp_path)
    monkeypatch.setenv("MCP_UI_DEPLOY_PYTHON", sys.executable)
    monkeypatch.setenv("MCP_UI_MAX_JOBS", "3")
    monkeypatch.setenv("TEST_PRIVATE_TOKEN", "hidden-environment-value")
    ui = tmp_path / "ui"
    ui.mkdir()
    projects = {}
    for name in ("alpha", "beta", "broken", "gamma"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "Dockerfile").write_text("FROM scratch")
        (folder / "mcp.json").write_text(json.dumps({"name": name, "mode": "stateless"}))
        projects[name] = {"title": name, "path": name, "configs": {"always-on": "mcp.json"}}
    (ui / "projects.json").write_text(json.dumps(projects))
    (ui / "runtime_metadata.json").write_text("{}")
    (ui / "trusted_repositories.json").write_text('{"repositories":[]}')
    (tmp_path / "deployctl.py").write_text('''import json,os,sys,time
cfg=json.load(open(sys.argv[sys.argv.index('--config')+1]))
for stage in ['BUILDING','PUSHING','DEPLOYING','VERIFYING']:
 print('@@MCP_DEPLOY_STAGE '+json.dumps({'job_id':os.environ['MCP_DEPLOY_JOB_ID'],'stage':stage,'message':'fixture'}),flush=True)
 print('only-job-'+os.environ['MCP_DEPLOY_JOB_ID'],flush=True)
 print(os.environ['TEST_PRIVATE_TOKEN'],flush=True)
 time.sleep(.06)
if cfg['name']=='broken':sys.exit(7)
print(json.dumps({'status':'healthy','name':cfg['name'],'namespace':'mcp-demo','image':'registry/'+cfg['name']+':'+os.environ['MCP_DEPLOY_JOB_ID']}),flush=True)
''')
    async def scenario():
        platform = application.Platform()
        # Conflicting alpha is queued before independent jobs; it must not block them.
        jobs = [await platform.enqueue(application.DeploymentRequest(project_id=name, source="build"))
                for name in ("alpha", "alpha", "beta", "broken", "gamma")]
        workers = [asyncio.create_task(platform.worker()) for _ in range(3)]
        peak = 0
        deadline = asyncio.get_running_loop().time() + 15
        while any(j["status"] in {"queued", "running"} for j in jobs):
            assert asyncio.get_running_loop().time() < deadline
            active = [j for j in jobs if j["status"] == "running"]
            peak = max(peak, len(active))
            assert len(active) <= 3 and len({j["name"] for j in active}) == len(active)
            await asyncio.sleep(.005)
        await platform.queue.close()
        await asyncio.gather(*workers)
        assert peak == 3
        assert jobs[2]["started_at"] < jobs[1]["started_at"]
        assert jobs[1]["started_at"] >= jobs[0]["finished_at"]
        assert [j["status"] for j in jobs] == ["succeeded", "succeeded", "succeeded", "failed", "succeeded"]
        assert jobs[3]["stage"] == "FAILED" and jobs[3]["exit_code"] == 7
        assert len({j["context_path"] for j in jobs}) == 5 and not list(platform.work_dir.iterdir())
        assert all(Path(j["command"][j["command"].index("--config") + 1]).parent == Path(j["context_path"]).parent for j in jobs)
        for job in jobs:
            log = platform.job_log(job["id"])
            assert "only-job-" + job["id"] in log and "hidden-environment-value" not in log
            for other in jobs:
                if other["id"] != job["id"]:
                    assert "only-job-" + other["id"] not in log
            persisted = json.loads((platform.jobs_dir / (job["id"] + ".json")).read_text())
            assert persisted["stage"] == job["stage"]
            assert persisted["source_snapshot"]["files"] == 2
        assert set(e["stage"] for e in jobs[0]["stage_history"]) == {"QUEUED","BUILDING","PUSHING","DEPLOYING","VERIFYING","SUCCEEDED"}
        reloaded = application.Platform()
        assert reloaded.get_job(jobs[3]["id"])["stage"] == "FAILED"
        assert "only-job-" + jobs[3]["id"] in reloaded.job_log(jobs[3]["id"])
    asyncio.run(scenario())
