#!/usr/bin/env python3
"""Concurrent HTTP load tester with percentile latency stats."""
from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Sample:
    ok: bool
    status: int
    latency_ms: float
    error: str = ""


@dataclass
class ScenarioResult:
    name: str
    url: str
    concurrency: int
    requests: int
    samples: list[Sample] = field(default_factory=list)

    @property
    def successes(self) -> int:
        return sum(1 for s in self.samples if s.ok)

    @property
    def failures(self) -> int:
        return len(self.samples) - self.successes

    @property
    def latencies(self) -> list[float]:
        return [s.latency_ms for s in self.samples]

    def percentile(self, p: float) -> float:
        vals = sorted(self.latencies)
        if not vals:
            return 0.0
        k = (len(vals) - 1) * (p / 100.0)
        f = int(k)
        c = min(f + 1, len(vals) - 1)
        if f == c:
            return vals[f]
        return vals[f] + (vals[c] - vals[f]) * (k - f)

    def status_breakdown(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for s in self.samples:
            out[s.status] = out.get(s.status, 0) + 1
        return dict(sorted(out.items()))


def do_request(method: str, url: str, headers: dict[str, str], body: bytes | None) -> Sample:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
            latency = (time.perf_counter() - start) * 1000
            return Sample(ok=200 <= resp.status < 400, status=resp.status, latency_ms=latency)
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except Exception:
            pass
        latency = (time.perf_counter() - start) * 1000
        return Sample(ok=False, status=e.code, latency_ms=latency, error=str(e.reason))
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return Sample(ok=False, status=0, latency_ms=latency, error=str(e))


def run_scenario(
    name: str,
    url: str,
    requests: int,
    concurrency: int,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
) -> ScenarioResult:
    hdrs = headers or {}
    result = ScenarioResult(name=name, url=url, concurrency=concurrency, requests=requests)
    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futs = [
            pool.submit(do_request, method, url, hdrs, body)
            for _ in range(requests)
        ]
        for fut in as_completed(futs):
            result.samples.append(fut.result())
    result.wall_seconds = time.perf_counter() - wall_start  # type: ignore[attr-defined]
    return result


def fmt(ms: float) -> str:
    return f"{ms:,.1f} ms"


def print_report(results: list[ScenarioResult]) -> None:
    print("\n" + "=" * 78)
    print("LOAD TEST REPORT")
    print("=" * 78)
    for r in results:
        wall = getattr(r, "wall_seconds", 0.0)
        rps = r.requests / wall if wall > 0 else 0
        lats = r.latencies
        print(f"\n### {r.name}")
        print(f"URL:          {r.url}")
        print(f"Requests:     {r.requests}  |  Concurrency: {r.concurrency}")
        print(f"Success:      {r.successes}/{r.requests} ({100 * r.successes / r.requests:.1f}%)")
        print(f"Failures:     {r.failures}")
        print(f"Status codes: {r.status_breakdown()}")
        print(f"Throughput:   {rps:,.1f} req/s")
        print(f"Wall time:    {wall:,.2f} s")
        if lats:
            print(
                "Latency:      "
                f"min={fmt(min(lats))}  "
                f"avg={fmt(statistics.mean(lats))}  "
                f"p50={fmt(r.percentile(50))}  "
                f"p95={fmt(r.percentile(95))}  "
                f"p99={fmt(r.percentile(99))}  "
                f"max={fmt(max(lats))}"
            )
    print("\n" + "=" * 78)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--app-id", default="RP-GREENVIEW")
    parser.add_argument("--rfid", default="ABC123456")
    args = parser.parse_args()

    base = args.base.rstrip("/")
    app_hdr = {"X-App-Id": args.app_id}
    login_body = json.dumps(
        {"email": "admin@greenview.com", "password": "admin123"}
    ).encode()

    # Warmup
    for _ in range(20):
        do_request("GET", f"{base}/healthz", {}, None)
        do_request("GET", f"{base}/api/v1/verify/rfid/{args.rfid}", app_hdr, None)

    scenarios: list[tuple[str, Callable[[], ScenarioResult]]] = [
        (
            "healthz_light",
            lambda: run_scenario("1) Health (light)", f"{base}/healthz", 500, 25),
        ),
        (
            "healthz_heavy",
            lambda: run_scenario("2) Health (heavy)", f"{base}/healthz", 2000, 100),
        ),
        (
            "ready",
            lambda: run_scenario("3) Ready (DB+Redis)", f"{base}/ready", 300, 30),
        ),
        (
            "tiers",
            lambda: run_scenario(
                "4) Subscription tiers (static)",
                f"{base}/api/v1/subscriptions/tiers",
                500,
                50,
            ),
        ),
        (
            "rfid_moderate",
            lambda: run_scenario(
                "5) RFID verify (moderate) — hot path",
                f"{base}/api/v1/verify/rfid/{args.rfid}",
                500,
                40,
                headers=app_hdr,
            ),
        ),
        (
            "rfid_heavy",
            lambda: run_scenario(
                "6) RFID verify (heavy) — hot path",
                f"{base}/api/v1/verify/rfid/{args.rfid}",
                1500,
                80,
                headers=app_hdr,
            ),
        ),
        (
            "login",
            lambda: run_scenario(
                "7) Auth login (bcrypt)",
                f"{base}/api/v1/auth/login",
                100,
                20,
                method="POST",
                headers={**app_hdr, "Content-Type": "application/json"},
                body=login_body,
            ),
        ),
        (
            "rfid_spike",
            lambda: run_scenario(
                "8) RFID verify (spike) — peak concurrency",
                f"{base}/api/v1/verify/rfid/{args.rfid}",
                800,
                150,
                headers=app_hdr,
            ),
        ),
    ]

    results: list[ScenarioResult] = []
    for key, runner in scenarios:
        print(f"Running {key}...", flush=True)
        res = runner()
        print(
            f"  done {res.name}: {res.successes}/{res.requests} ok, "
            f"{getattr(res, 'wall_seconds', 0):.2f}s, "
            f"p95={res.percentile(95):.1f}ms",
            flush=True,
        )
        results.append(res)

    print_report(results)

    summary = {
        r.name: {
            "requests": r.requests,
            "concurrency": r.concurrency,
            "success_rate": round(r.successes / r.requests, 4),
            "rps": round(r.requests / getattr(r, "wall_seconds", 1), 1),
            "p50_ms": round(r.percentile(50), 1),
            "p95_ms": round(r.percentile(95), 1),
            "p99_ms": round(r.percentile(99), 1),
            "max_ms": round(max(r.latencies), 1) if r.latencies else 0,
            "statuses": r.status_breakdown(),
        }
        for r in results
    }
    out_path = "/home/bash/Desktop/Residence-backend/scripts/load_test_results.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"JSON written to {out_path}")


if __name__ == "__main__":
    main()
