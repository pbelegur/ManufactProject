"""Offline analysis; never sends traffic or changes Kubernetes resources."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys

from validate_final_performance import summary


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def watch(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("{"):
            yield json.loads(line)


def analyze(rollout_folder, warm_folder):
    warm = load(warm_folder / "warm.json")
    traffic = load(rollout_folder / "traffic.json")
    operation = load(rollout_folder / "rollout-operation.json")
    out = {"warm_source": str(warm_folder), "rollout_source": str(rollout_folder),
           "warm_summary": warm["summary"], "warm_protocols": sorted({c["protocol_version"] for c in warm["connections"] if c["label"] == "warm-pooled"}),
           "warm_connections": [], "warm_http_breakdown": [],
           "traffic_summary": traffic["summary"], "rollout": operation,
           "rollout_seconds": stamp(operation["end"]) - stamp(operation["start"])}
    for route in sorted({c["route"] for c in warm["connections"]}):
        fresh = [c for c in warm["connections"] if c["route"] == route and c["label"] == "fresh-warm"]
        records = [r for r in warm["requests"] if r["route"] == route and r["phase"] == "fresh-warm"]
        out["warm_connections"].append({"route": route, "protocols": sorted({c["protocol_version"] for c in fresh}),
            **{key: summary([c[key] for c in fresh]) for key in ["factory_ms", "mcp_initialize_ms", "mcp_teardown_ms", "connection_total_ms"]},
            "first_call_ms": summary([r["ms"] for r in records]),
            "whole_connection_ms": summary([r["whole_connection_ms"] for r in records])})
        for tool in sorted({r["tool"] for r in warm["requests"] if r["route"] == route and r["phase"] == "warm"}):
            records = [r for r in warm["requests"] if r["route"] == route and r["phase"] == "warm" and r["tool"] == tool]
            http_records, residuals = [], []
            for record in records:
                begin = stamp(record["utc"])
                candidates = [h for h in warm["http"] if h["route"] == route and h.get("rpc_method") == "tools/call" and
                              h.get("tool") == tool and begin <= stamp(h["utc"]) <= begin + record["ms"] / 1000 + .001]
                assert len(candidates) == 1, (route, record, candidates)
                h = candidates[0]
                http_records.append(h)
                residuals.append(record["ms"] - h["body_complete_ms"])
            out["warm_http_breakdown"].append({"route": route, "tool": tool,
                "request_to_headers": summary([h["headers_ms"] for h in http_records]),
                "request_to_body_complete": summary([h["body_complete_ms"] for h in http_records]),
                "paired_sdk_outside_http": summary(residuals)})
    all_traffic = [r for r in traffic["requests"] if r["phase"] == "rollout"]
    out["traffic_window"] = {"first_sent_utc": min(r["utc"] for r in all_traffic),
                             "last_sent_utc": max(r["utc"] for r in all_traffic),
                             "attempts": len(all_traffic), "failures": sum(not r["success"] for r in all_traffic)}
    out["traffic_during_cli"] = [{"route": route, "attempts": len(records := [r for r in all_traffic if r["route"] == route and
         stamp(operation["start"]) <= stamp(r["utc"]) <= stamp(operation["end"])]),
         "successes": sum(r["success"] for r in records), "failures": sum(not r["success"] for r in records)}
         for route in ["knative-stateless", "knative-stateful"]]
    out["traffic_http_statuses"] = [{"method": method, "status": status, "count": count} for (method, status), count in
                                    Counter((r.get("rpc_method"), r.get("status")) for r in traffic["http"]).items()]
    gateway = (rollout_folder / "3scale-kourier-gateway.log").read_text().splitlines()
    ids = {r["request_id"] for r in traffic["http"]}
    matched = [line for line in gateway if any(i in line for i in ids)]
    (rollout_folder / "correlated-gateway.log").write_text("\n".join(matched) + "\n", encoding="utf-8")
    out["gateway_correlation"] = {"client_http_requests": len(ids), "matched_gateway_lines": len(matched),
        "matched_5xx_lines": [line for line in matched if re.search(r'HTTP/[^\"]+" 5\d\d ', line)],
        "all_5xx_lines": [line for line in gateway if re.search(r'HTTP/[^\"]+" 5\d\d ', line)]}
    out["endpoints"] = []
    for event in watch(rollout_folder / "endpointslices-watch.json"):
        obj = event["object"]
        name = obj["metadata"]["labels"].get("kubernetes.io/service-name", "")
        if name in {"stateless-demo-00014-private", "stateless-demo-00015-private"}:
            out["endpoints"].append({"event": event["type"], "service": name,
                "resource_version": obj["metadata"]["resourceVersion"],
                "managed_field_update_utc": max((f.get("time", "") for f in obj["metadata"].get("managedFields", [])), default=""),
                "endpoints": [{"ip": e["addresses"], "pod": e.get("targetRef", {}).get("name"),
                               **e.get("conditions", {})} for e in (obj.get("endpoints") or [])]})
    out["pod_transitions"] = []
    seen = set()
    for event in watch(rollout_folder / "pods-watch.json"):
        obj = event["object"]
        revision = obj["metadata"].get("labels", {}).get("serving.knative.dev/revision")
        if revision in {"stateless-demo-00014", "stateless-demo-00015"}:
            ready = next((c for c in obj.get("status", {}).get("conditions", []) if c["type"] == "Ready"), {})
            record = {"pod": obj["metadata"]["name"], "revision": revision,
                      "created": obj["metadata"]["creationTimestamp"], "deleting": obj["metadata"].get("deletionTimestamp"),
                      "ready": ready.get("status"), "ready_transition": ready.get("lastTransitionTime"),
                      "ip": obj.get("status", {}).get("podIP")}
            key = json.dumps(record, sort_keys=True)
            if key not in seen:
                out["pod_transitions"].append(record)
                seen.add(key)
    before, after = load(rollout_folder / "before.json"), load(warm_folder / "after.json")
    out["preservation"] = []
    for name in ["stateless-demo", "stateful-demo", "stateful-demo-redis", "contract-probe"]:
        old = next(o for o in before["mcp-demo"]["items"] if o["kind"] == "Deployment" and o["metadata"]["name"] == name)
        new = next(o for o in after["mcp-demo"]["items"] if o["kind"] == "Deployment" and o["metadata"]["name"] == name)
        ready_endpoints = [e for s in after["mcp-demo"]["items"] if s["kind"] == "EndpointSlice" and
                           s["metadata"]["labels"].get("kubernetes.io/service-name") == name
                           for e in (s.get("endpoints") or []) if e.get("conditions", {}).get("ready")]
        old_uids = {o["metadata"]["uid"] for o in before["mcp-demo"]["items"] if o["kind"] == "Pod" and o["metadata"].get("labels", {}).get("app") == name}
        new_uids = {o["metadata"]["uid"] for o in after["mcp-demo"]["items"] if o["kind"] == "Pod" and o["metadata"].get("labels", {}).get("app") == name}
        out["preservation"].append({"deployment": name, "same_uid": old["metadata"]["uid"] == new["metadata"]["uid"],
            "same_image": old["spec"]["template"]["spec"]["containers"][0]["image"] == new["spec"]["template"]["spec"]["containers"][0]["image"],
            "same_pod_uids": old_uids == new_uids, "ready": new["status"].get("readyReplicas", 0),
            "desired": new["spec"]["replicas"], "ready_endpoints": len(ready_endpoints)})
    out["ui_errors"] = after["ui"]["errors"]
    out["ui_services"] = [i for i in after["ui"]["items"] if i["name"] in {"stateless-demo", "stateful-demo"}]
    out["counter_checks"] = warm["counter_checks"]
    out["body_diagnostics"] = [load(rollout_folder / ("body-" + mode + ".json")) for mode in ["stateless", "stateful"]]
    out["request_phases"] = dict(Counter(r["phase"] for r in warm["requests"]))
    out["warm_all_requests"] = len(warm["requests"])
    out["warm_all_failures"] = sum(not r["success"] for r in warm["requests"])
    out["cpu_before"] = warm["cpu_before"]
    out["cpu_after"] = warm["cpu_after"]
    out["validation_checks"] = {
        "matching_warm_protocol": len(out["warm_protocols"]) == 1,
        "all_warm_tools_succeeded": out["warm_all_failures"] == 0,
        "rollout_succeeded": operation["exit_code"] == 0,
        "all_measured_rollout_requests_succeeded": out["traffic_window"]["failures"] == 0,
        "gateway_requests_correlated": len(matched) == len(ids),
        "ui_query_succeeded": not out["ui_errors"],
        "always_on_resources_preserved": all(r["same_uid"] and r["same_image"] and r["same_pod_uids"] and
            r["ready"] == r["desired"] and r["ready_endpoints"] == r["desired"] for r in out["preservation"]),
        "redis_counter_checks_succeeded": all(r["success"] and r["value"] == warm["samples_per_tool_route"] + 5
                                              for r in out["counter_checks"]),
    }
    (warm_folder / "analysis.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps({k: out[k] for k in ["warm_protocols", "traffic_window", "traffic_during_cli", "gateway_correlation", "preservation", "warm_all_requests", "warm_all_failures"]}, indent=2))
    assert all(out["validation_checks"].values()), out["validation_checks"]


if __name__ == "__main__":
    analyze(Path(sys.argv[1]), Path(sys.argv[2]))
