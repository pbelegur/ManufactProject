"""Status classification from observed Kubernetes fields, not job optimism."""
import re
from urllib.parse import urlsplit


def health(backend, desired, ready, resource, pods):
    conditions = resource.get("status", {}).get("conditions", [])
    terminal = any(c.get("status") == "False" and c.get("reason") in
                   {"ProgressDeadlineExceeded", "RevisionFailed", "RevisionMissing", "RouteFailed"} for c in conditions)
    bad_container = any(c.get("state", {}).get("waiting", {}).get("reason") in
                        {"CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError"}
                        for pod in pods if not pod["metadata"].get("deletionTimestamp")
                        for c in pod.get("status", {}).get("containerStatuses", []))
    observed = resource.get("status", {}).get("observedGeneration", 0) >= resource["metadata"].get("generation", 1)
    route_ready = any(c["type"] == "Ready" and c["status"] == "True" for c in conditions)
    if terminal or bad_container:
        return "Degraded" if ready else "Failed"
    if not observed:
        return "Deploying"
    if backend == "knative" and not route_ready:
        if any(c["type"] == "Ready" and c["status"] == "False" for c in conditions):
            return "Degraded" if ready else "Failed"
        return "Deploying"
    if desired == ready == 0:
        return "Idle/Scaled-to-zero"
    if desired != ready:
        if backend == "knative":
            return "Scaling"
        progressing = next((c for c in conditions if c["type"] == "Progressing"), {})
        if progressing.get("reason") in {"ReplicaSetUpdated", "NewReplicaSetCreated", "FoundNewReplicaSet"}:
            return "Deploying"
        return "Degraded"
    if backend == "always-on" and resource.get("status", {}).get("updatedReplicas", 0) != desired:
        return "Deploying"
    if backend == "always-on" and resource.get("status", {}).get("availableReplicas", 0) < desired:
        return "Degraded"
    return "Healthy"


def redact(text):
    text = re.sub(r"(redis(?:s)?://)[^/@\s]+@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)((?:password|secret|token|api_key|authorization)\s*[=:]\s*)[^\s,}]+", r"\1[redacted]", text)
    text = re.sub(r"AKIA[A-Z0-9]{16}", "[redacted-access-key]", text)
    return text


def job_matches_runtime(job, row):
    if job["status"] != "succeeded":
        return False
    result = job.get("deployment_result", {})
    if row["backend"] == "knative":
        # The same image can have several revisions (including terminal-CLI
        # rollouts). Image equality alone cannot establish deployment time.
        routed = {t.get("revisionName") for t in row.get("traffic", []) if t.get("percent")}
        return bool(result.get("revision") in routed and result.get("image") == row["image"])
    if result.get("image") == row["image"]:
        return True
    command = job["command"]
    return (("--image" in command and command[command.index("--image") + 1] == row["image"]) or
            (job["source"] == "build" and ":" + job["version"] + "-" in row["image"]))


def redis_view(container, services):
    setting = next((e for e in container.get("env", []) if e["name"] == "REDIS_URL"), None)
    if not setting:
        return None
    if "value" not in setting:
        return {"configuration": "Secret/config reference; value is not exposed"}
    try:
        parsed = urlsplit(setting["value"])
        host = parsed.hostname
        return {"host": host, "port": parsed.port or 6379, "database": parsed.path.lstrip("/") or "0",
                "credentials": "Configured; hidden" if parsed.username or parsed.password else "Not present in URL",
                "service_present": bool(host and host.split(".")[0] in services),
                "configuration": "Shared Redis outside MCP pods; no in-process application state"}
    except ValueError:
        return {"configuration": "Invalid Redis URL; value is not exposed"}
