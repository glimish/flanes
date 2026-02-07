"""
Shared pytest configuration and fixtures.

On Windows CI runners, a spurious KeyboardInterrupt is delivered to the
main thread during long-running tests (likely from the runner's process
management or a stale signal).  All tests actually pass, but pytest
sees the KeyboardInterrupt and exits with code 1.

The workaround: ignore SIGINT entirely on Windows CI.  We don't need
interactive interrupt handling in CI, and this prevents the spurious
signal from aborting a green test run.
"""

import os
import signal

_WINDOWS_CI = os.name == "nt" and os.environ.get("CI") == "true"


def pytest_configure(config):
    """Ignore SIGINT on Windows CI to prevent spurious KeyboardInterrupt."""
    if _WINDOWS_CI:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (OSError, ValueError):
            pass
