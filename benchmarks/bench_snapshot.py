"""
Benchmark: Snapshot Performance

Measures snapshot timing across different scenarios:
1. Initial snapshot (all files new)
2. No-change snapshot (stat cache hit)
3. Small-change snapshot (few files modified)

Usage:
    python -m benchmarks.bench_snapshot --files 10000 --dirs 100 --rounds 3
"""

import argparse
import json
import random
import shutil
import sys
import time
from pathlib import Path

# Ensure the project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from vex.repo import Repository


def generate_files(root: Path, num_files: int, num_dirs: int):
    """Generate N files spread across D directories."""
    dirs = [root]
    for i in range(num_dirs):
        d = root / f"dir_{i:04d}"
        d.mkdir(parents=True, exist_ok=True)
        dirs.append(d)

    files = []
    for i in range(num_files):
        d = dirs[i % len(dirs)]
        fp = d / f"file_{i:06d}.txt"
        content = f"file-{i}-content-{random.randint(0, 999999)}\n" * 10
        fp.write_text(content)
        files.append(fp)

    return files


def run_benchmark(num_files: int, num_dirs: int, rounds: int):
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="vex_bench_"))
    project = tmpdir / "project"
    project.mkdir()

    print(f"Generating {num_files} files across {num_dirs} directories...")
    generate_files(project, num_files, num_dirs)

    print("Initializing vex repo...")
    repo = Repository.init(project)
    ws_path = repo.workspace_path("main")

    results = {}

    # Benchmark 1: Initial snapshot
    timings = []
    for r in range(rounds):
        t0 = time.monotonic()
        repo.snapshot("main")
        elapsed = time.monotonic() - t0
        timings.append(elapsed)
        print(f"  Initial snapshot round {r+1}: {elapsed:.3f}s")
    results["initial_snapshot"] = {
        "mean": sum(timings) / len(timings),
        "min": min(timings),
        "max": max(timings),
    }

    # Benchmark 2: No-change snapshot (should hit stat cache)
    timings = []
    for r in range(rounds):
        t0 = time.monotonic()
        repo.snapshot("main")
        elapsed = time.monotonic() - t0
        timings.append(elapsed)
        print(f"  No-change snapshot round {r+1}: {elapsed:.3f}s")
    results["no_change_snapshot"] = {
        "mean": sum(timings) / len(timings),
        "min": min(timings),
        "max": max(timings),
    }

    # Benchmark 3: Small-change snapshot (3 files modified)
    timings = []
    for r in range(rounds):
        # Modify 3 random files in the workspace
        ws_files = list(ws_path.rglob("file_*.txt"))
        if len(ws_files) >= 3:
            for f in random.sample(ws_files, 3):
                f.write_text(f"modified-round-{r}-{random.randint(0, 999999)}\n")

        t0 = time.monotonic()
        repo.snapshot("main")
        elapsed = time.monotonic() - t0
        timings.append(elapsed)
        print(f"  Small-change snapshot round {r+1}: {elapsed:.3f}s")
    results["small_change_snapshot"] = {
        "mean": sum(timings) / len(timings),
        "min": min(timings),
        "max": max(timings),
    }

    # Storage stats
    stats = repo.store.stats()
    results["storage"] = stats

    print(f"\n{'='*60}")
    print(f"RESULTS ({num_files} files, {num_dirs} dirs, {rounds} rounds)")
    print(f"{'='*60}")
    for name, timing in results.items():
        if name == "storage":
            print("\nStorage:")
            print(f"  Objects: {timing['total_objects']}")
            print(f"  Bytes:   {timing['total_bytes']:,}")
            if timing.get("by_type"):
                for typ, info in timing["by_type"].items():
                    print(f"  {typ}: {info['count']} objects, {info['bytes']:,} bytes")
        else:
            print(f"\n{name}:")
            print(f"  Mean: {timing['mean']:.3f}s")
            print(f"  Min:  {timing['min']:.3f}s")
            print(f"  Max:  {timing['max']:.3f}s")

    repo.close()
    shutil.rmtree(tmpdir, ignore_errors=True)
    return results


def main():
    parser = argparse.ArgumentParser(description="Vex snapshot benchmark")
    parser.add_argument("--files", type=int, default=1000, help="Number of files")
    parser.add_argument("--dirs", type=int, default=50, help="Number of directories")
    parser.add_argument("--rounds", type=int, default=3, help="Rounds per benchmark")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    args = parser.parse_args()

    results = run_benchmark(args.files, args.dirs, args.rounds)

    if args.json:
        output = {
            "benchmark": "snapshot",
            "params": {"files": args.files, "dirs": args.dirs, "rounds": args.rounds},
            "results": results,
        }
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
