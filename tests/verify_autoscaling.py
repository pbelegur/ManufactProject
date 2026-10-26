"""Sustained real MCP concurrency, automatic scale-up/zero, and cold starts."""
import argparse
import asyncio
import time
from uuid import uuid4

from knative_common import Cluster, call, save


async def verify(cluster, gateway, name):
    host = cluster.get(f"ksvc/{name}")["status"]["url"].split("://")[1]
    stateful = name == "stateful-demo"
    key = "autoscale_" + uuid4().hex
    successes, failures, latencies, samples = [], [], [], []
    expected = 0
    if stateful:
        value, latency = await call(gateway, host, "increment_counter", {"name": key, "amount": 7})
        assert value == 7
        expected = 7
    else:
        value, latency = await call(gateway, host, "echo", {"message": "warmup"})
        assert value == "warmup"
    started = time.monotonic()
    first_above_two = None

    async def worker():
        deadline = started + 65
        while time.monotonic() < deadline:
            try:
                tool = "increment_counter" if stateful else "echo"
                arguments = {"name": key, "delay_seconds": 3} if stateful else {"message": "concurrent", "delay_seconds": 3}
                value, latency = await call(gateway, host, tool, arguments)
                assert isinstance(value, int) if stateful else value == "concurrent"
                successes.append(value)
                latencies.append(latency)
            except Exception as exc:
                failures.append(f"{type(exc).__name__}: {exc}")

    tasks = [asyncio.create_task(worker()) for _ in range(8)]
    while any(not task.done() for task in tasks):
        counts = await asyncio.to_thread(cluster.counts, name)
        elapsed = time.monotonic() - started
        samples.append({"seconds": round(elapsed, 3), **counts})
        if counts["ready"] > 2 and first_above_two is None:
            first_above_two = elapsed
        print(name, samples[-1], flush=True)
        await asyncio.sleep(2)
    await asyncio.gather(*tasks)
    expected += len(successes) if stateful else 0
    load_result = {"successes": len(successes), "failures": failures, "latency_seconds": latencies,
                   "scale_up_seconds": first_above_two, "samples": samples}
    save(name + "-load.json", load_result)
    assert first_above_two is not None, "no automatically ready replica count above two observed"
    assert not failures, failures
    idle_start = time.monotonic()
    idle_samples = []
    while True:
        counts = await asyncio.to_thread(cluster.counts, name)
        sample = {"seconds": round(time.monotonic() - idle_start, 3), **counts}
        idle_samples.append(sample)
        print("idle", name, sample, flush=True)
        if counts["desired"] == counts["all_pods"] == 0:
            break
        if time.monotonic() - idle_start > 240:
            save(name + "-idle-failure.json", idle_samples)
            raise RuntimeError("service did not automatically reach zero")
        await asyncio.sleep(3)
    zero_time = time.monotonic() - idle_start
    # No direct pod/Revision connection: this request goes through Kourier and
    # the activator while there are no application pods.
    value, cold_latency = await call(gateway, host, "get_counter" if stateful else "echo",
                                     {"name": key} if stateful else {"message": "cold-start"})
    assert value == (expected if stateful else "cold-start"), (value, expected)
    awake = await asyncio.to_thread(cluster.counts, name)
    result = {"name": name, "result": "PASS", "host": host, "load": load_result,
              "idle_samples": idle_samples, "scale_to_zero_seconds": zero_time,
              "cold_start_seconds": cold_latency, "cold_start_value": value, "awake_counts": awake,
              "counter": key if stateful else None, "expected_counter": expected if stateful else None}
    save(name + "-autoscaling.json", result)
    print({k: v for k, v in result.items() if k not in ("load", "idle_samples")}, flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubeconfig", required=True)
    args = parser.parse_args()
    cluster = Cluster(args.kubeconfig)
    with cluster.gateway() as gateway:
        for name in ("stateless-demo", "stateful-demo"):
            asyncio.run(verify(cluster, gateway, name))


if __name__ == "__main__":
    main()
