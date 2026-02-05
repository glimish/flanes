"""
Benchmark: Throughput and Latency

Measures:
1. Snapshot latency across different file counts (p50, p95, p99)
2. Commit throughput (commits/second)
3. REST API requests/second by concurrent client count

Usage:
    python -m benchmarks.bench_throughput
    python -m benchmarks.bench_throughput --scenario snapshot --files 10000
    python -m benchmarks.bench_throughput --scenario commit --duration 30
    python -m benchmarks.bench_throughput --scenario api --clients 50
"""

import argparse
import random
import shutil
import socket
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from vex.repo import Repository
from vex.state import AgentIdentity


def percentile(data, p):
    """Calculate percentile of data."""
    if not data:
        return 0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100)
    f = int(k)
    c = f + 1 if f + 1 < len(sorted_data) else f
    return sorted_data[f] + (sorted_data[c] - sorted_data[f]) * (k - f)


def generate_workspace(path: Path, num_files: int, num_dirs: int = 50):
    """Generate files in a workspace."""
    dirs = [path]
    for i in range(num_dirs):
        d = path / f"dir_{i:04d}"
        d.mkdir(parents=True, exist_ok=True)
        dirs.append(d)

    for i in range(num_files):
        d = dirs[i % len(dirs)]
        content = f"file-{i}-content-{random.randint(0, 999999)}\n" * 10
        (d / f"file_{i:06d}.txt").write_text(content)


def bench_snapshot_latency(file_counts=None, samples=20):
    """Measure snapshot latency across different file counts."""
    if file_counts is None:
        file_counts = [100, 1000, 5000, 10000]

    results = {}

    for count in file_counts:
        tmpdir = Path(tempfile.mkdtemp(prefix="vex_bench_"))
        project = tmpdir / "project"
        project.mkdir()

        print(f"\nGenerating {count} files...")
        generate_workspace(project, count, num_dirs=max(10, count // 100))

        print("Initializing repository...")
        repo = Repository.init(project)

        # First snapshot to warm up
        repo.snapshot("main")

        latencies = []
        print(f"Running {samples} snapshot samples...")

        for i in range(samples):
            # Modify a few files to ensure work happens
            ws = repo.workspace_path("main")
            for j in range(3):
                mod_file = ws / f"dir_000{j}" / f"modified_{i}_{j}.txt"
                mod_file.parent.mkdir(exist_ok=True)
                mod_file.write_text(f"Modified {i}-{j}\n")

            t0 = time.monotonic()
            repo.snapshot("main")
            elapsed_ms = (time.monotonic() - t0) * 1000
            latencies.append(elapsed_ms)

        repo.close()
        shutil.rmtree(tmpdir, ignore_errors=True)

        results[count] = {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "mean": statistics.mean(latencies),
            "min": min(latencies),
            "max": max(latencies),
        }

        print(f"  {count} files: p50={results[count]['p50']:.1f}ms, "
              f"p95={results[count]['p95']:.1f}ms, p99={results[count]['p99']:.1f}ms")

    return results


def bench_commit_throughput(duration_seconds=30, num_lanes=10):
    """Measure commits/second with multiple concurrent agents."""
    tmpdir = Path(tempfile.mkdtemp(prefix="vex_bench_"))
    project = tmpdir / "project"
    project.mkdir()

    print(f"Setting up with {num_lanes} lanes...")
    (project / "README.md").write_text("# Benchmark\n")
    repo = Repository.init(project)
    initial_head = repo.head()
    repo_root = repo.root

    # Create lanes
    for i in range(num_lanes):
        repo.create_lane(f"lane-{i}", base=initial_head)

    commits = []
    errors = []
    lock = threading.Lock()
    stop_flag = threading.Event()

    def commit_worker(lane_num):
        thread_repo = Repository(repo_root)
        agent = AgentIdentity(agent_id=f"agent-{lane_num}", agent_type="benchmark")
        commit_num = 0

        try:
            while not stop_flag.is_set():
                ws = thread_repo.workspace_path(f"lane-{lane_num}")
                (ws / f"commit_{commit_num}.txt").write_text(f"Commit {commit_num}\n")

                try:
                    thread_repo.quick_commit(
                        workspace=f"lane-{lane_num}",
                        prompt=f"Commit {commit_num}",
                        agent=agent,
                        auto_accept=True,
                    )
                    with lock:
                        commits.append(time.monotonic())
                    commit_num += 1
                except Exception as e:
                    with lock:
                        errors.append(str(e))
        finally:
            thread_repo.close()

    print(f"Running for {duration_seconds} seconds...")
    t0 = time.monotonic()

    with ThreadPoolExecutor(max_workers=num_lanes) as executor:
        futures = [executor.submit(commit_worker, i) for i in range(num_lanes)]

        time.sleep(duration_seconds)
        stop_flag.set()

        for f in futures:
            try:
                f.result(timeout=5)
            except Exception:
                pass

    elapsed = time.monotonic() - t0
    repo.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    throughput = len(commits) / elapsed if elapsed > 0 else 0

    print("\nResults:")
    print(f"  Total commits: {len(commits)}")
    print(f"  Duration: {elapsed:.1f}s")
    print(f"  Throughput: {throughput:.2f} commits/second")
    print(f"  Errors: {len(errors)}")

    return {
        "commits": len(commits),
        "duration": elapsed,
        "throughput": throughput,
        "errors": len(errors),
    }


def bench_rest_api_rps(concurrent_clients=None, duration_seconds=10):
    """Measure REST API requests/second by client count."""
    if concurrent_clients is None:
        concurrent_clients = [1, 5, 10, 25, 50]

    import json
    import urllib.request

    from vex.server import VexServer

    tmpdir = Path(tempfile.mkdtemp(prefix="vex_bench_"))
    project = tmpdir / "project"
    project.mkdir()

    (project / "README.md").write_text("# Benchmark\n")
    repo = Repository.init(project)

    # Find free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]

    server = VexServer(repo, "127.0.0.1", port)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.2)  # Let server start

    results = {}

    for num_clients in concurrent_clients:
        requests_made = []
        errors = []
        lock = threading.Lock()
        stop_flag = threading.Event()

        def client_worker():
            while not stop_flag.is_set():
                try:
                    url = f"http://127.0.0.1:{port}/status"
                    with urllib.request.urlopen(url, timeout=5) as resp:
                        json.loads(resp.read())
                    with lock:
                        requests_made.append(time.monotonic())
                except Exception as e:
                    with lock:
                        errors.append(str(e))

        print(f"\nTesting with {num_clients} concurrent clients...")
        t0 = time.monotonic()

        with ThreadPoolExecutor(max_workers=num_clients) as executor:
            futures = [executor.submit(client_worker) for _ in range(num_clients)]

            time.sleep(duration_seconds)
            stop_flag.set()

            for f in futures:
                try:
                    f.result(timeout=2)
                except Exception:
                    pass

        elapsed = time.monotonic() - t0
        rps = len(requests_made) / elapsed if elapsed > 0 else 0

        results[num_clients] = {
            "requests": len(requests_made),
            "duration": elapsed,
            "rps": rps,
            "errors": len(errors),
        }

        print(f"  Clients: {num_clients}, RPS: {rps:.1f}, Errors: {len(errors)}")

    server.shutdown()
    repo.close()
    shutil.rmtree(tmpdir, ignore_errors=True)

    return results


def main():
    parser = argparse.ArgumentParser(description="Vex throughput benchmarks")
    parser.add_argument("--scenario", choices=["snapshot", "commit", "api", "all"],
                        default="all", help="Which benchmark to run")
    parser.add_argument("--files", type=int, nargs="+", default=[100, 1000, 5000],
                        help="File counts for snapshot benchmark")
    parser.add_argument("--duration", type=int, default=30,
                        help="Duration in seconds for throughput tests")
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 5, 10, 25],
                        help="Client counts for API benchmark")
    parser.add_argument("--samples", type=int, default=20,
                        help="Samples for latency measurements")
    args = parser.parse_args()

    print("=" * 60)
    print("Vex Throughput Benchmarks")
    print("=" * 60)

    all_results = {}

    if args.scenario in ("snapshot", "all"):
        print("\n--- Snapshot Latency ---")
        all_results["snapshot"] = bench_snapshot_latency(args.files, args.samples)

    if args.scenario in ("commit", "all"):
        print("\n--- Commit Throughput ---")
        all_results["commit"] = bench_commit_throughput(args.duration)

    if args.scenario in ("api", "all"):
        print("\n--- REST API RPS ---")
        all_results["api"] = bench_rest_api_rps(args.clients)

    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)

    if "snapshot" in all_results:
        print("\nSnapshot Latency (ms):")
        for count, data in all_results["snapshot"].items():
            p50, p95, p99 = data['p50'], data['p95'], data['p99']
            print(f"  {count:6d} files: p50={p50:7.1f}  p95={p95:7.1f}  p99={p99:7.1f}")

    if "commit" in all_results:
        print(f"\nCommit Throughput: {all_results['commit']['throughput']:.2f} commits/sec")

    if "api" in all_results:
        print("\nREST API RPS:")
        for clients, data in all_results["api"].items():
            print(f"  {clients:3d} clients: {data['rps']:7.1f} req/sec")


if __name__ == "__main__":
    main()
