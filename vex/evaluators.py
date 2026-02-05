"""
Evaluation Plugins

Runs shell commands (pytest, ruff, etc.) as evaluators via subprocess.
Evaluator configs are stored in .vex/config.json under "evaluators".
"""

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .state import EvaluationResult


@dataclass
class EvaluatorConfig:
    """Configuration for a single evaluator.

    Fix #6 from audit: Supports both `command` (string, OS-dependent parsing)
    and `args` (explicit list, cross-platform). If both are provided, `args`
    takes precedence.
    """
    name: str
    command: str = ""
    args: list[str] | None = None  # Fix #6: Explicit args list for cross-platform
    working_directory: str | None = None
    required: bool = True
    timeout_seconds: int = 300

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "working_directory": self.working_directory,
            "required": self.required,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.args:
            d["args"] = self.args
        if self.command:
            d["command"] = self.command
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EvaluatorConfig":
        return cls(
            name=d["name"],
            command=d.get("command", ""),
            args=d.get("args"),
            working_directory=d.get("working_directory"),
            required=d.get("required", True),
            timeout_seconds=d.get("timeout_seconds", 300),
        )


@dataclass
class EvaluatorResult:
    """Result from running a single evaluator."""
    name: str
    passed: bool
    returncode: int
    stdout: str
    stderr: str
    duration_ms: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
        }


def load_evaluators(config: dict) -> list:
    """Load evaluator configs from a config dict."""
    evaluators_data = config.get("evaluators", [])
    return [EvaluatorConfig.from_dict(e) for e in evaluators_data]


def run_evaluator(evaluator: EvaluatorConfig, workspace_path: Path) -> EvaluatorResult:
    """Run a single evaluator command in the workspace directory.

    Fix #6 from audit: If `args` is provided, uses it directly (cross-platform).
    Otherwise falls back to OS-dependent command parsing.
    """
    cwd = workspace_path
    if evaluator.working_directory:
        cwd = workspace_path / evaluator.working_directory
        # Validate working_directory stays within workspace
        try:
            cwd.resolve().relative_to(workspace_path.resolve())
        except ValueError:
            return EvaluatorResult(
                name=evaluator.name,
                passed=False,
                returncode=-1,
                stdout="",
                stderr=f"Evaluator working_directory '{evaluator.working_directory}' escapes workspace",
                duration_ms=0.0,
            )

    start = time.monotonic()
    try:
        # Fix #6: Use explicit args if provided, otherwise parse command
        cmd: str | list[str]
        if evaluator.args:
            # Explicit args list — cross-platform, no parsing needed
            cmd = evaluator.args
        elif evaluator.command:
            # Legacy: On Windows, pass command as string (CreateProcess handles it natively).
            # On POSIX, split into list to avoid shell interpretation.
            cmd = evaluator.command if os.name == "nt" else shlex.split(evaluator.command)
        else:
            return EvaluatorResult(
                name=evaluator.name,
                passed=False,
                returncode=-1,
                stdout="",
                stderr=f"Evaluator '{evaluator.name}' has no command or args specified",
                duration_ms=0.0,
            )

        result = subprocess.run(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=evaluator.timeout_seconds,
        )
        duration_ms = (time.monotonic() - start) * 1000
        return EvaluatorResult(
            name=evaluator.name,
            passed=result.returncode == 0,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        return EvaluatorResult(
            name=evaluator.name,
            passed=False,
            returncode=-1,
            stdout="",
            stderr=f"Evaluator '{evaluator.name}' timed out after {evaluator.timeout_seconds}s",
            duration_ms=duration_ms,
        )


def run_all_evaluators(evaluators: list, workspace_path: Path) -> EvaluationResult:
    """Run all evaluators and return an aggregate EvaluationResult."""
    results = []
    checks = {}
    all_passed = True
    total_duration = 0.0

    for evaluator in evaluators:
        result = run_evaluator(evaluator, workspace_path)
        results.append(result)
        checks[evaluator.name] = result.passed
        total_duration += result.duration_ms

        if not result.passed and evaluator.required:
            all_passed = False

    summaries = []
    for r in results:
        status = "passed" if r.passed else "FAILED"
        summaries.append(f"{r.name}: {status}")

    return EvaluationResult(
        passed=all_passed,
        evaluator="plugin_runner",
        checks=checks,
        summary="; ".join(summaries),
        duration_ms=total_duration,
        metadata={"results": [r.to_dict() for r in results]},
    )
