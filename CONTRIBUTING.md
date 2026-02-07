# Contributing to Fla

## Development Setup

```bash
# Clone the repository
git clone https://github.com/glimish/fla.git
cd fla

# Install in development mode with all dev dependencies
pip install -e ".[dev]"
```

## Running Tests

```bash
# Run all tests
python -X utf8 -m pytest tests/ -v

# Run fast tests only (skip stress/slow)
python -X utf8 -m pytest tests/ -v -m "not stress and not slow"

# Run a specific test file
python -X utf8 -m pytest tests/test_cli.py -v
```

## Linting and Type Checking

```bash
# Lint
ruff check fla/ tests/

# Check formatting
ruff format --check fla/ tests/

# Auto-format
ruff format fla/ tests/

# Type check
mypy fla/
```

## Code Style

- Python 3.10+ (use `X | Y` union syntax, not `Union[X, Y]`)
- Max line length: 100 characters (configured in `pyproject.toml`)
- Linting: ruff with `E, F, W, I, N, UP` rules
- No unused imports or variables

## Pull Request Guidelines

1. Fork the repo and create a branch from `main`
2. Write tests for new functionality
3. Run the full test suite and ensure it passes
4. Run `ruff check` and `mypy` with no new errors
5. Write a clear PR description explaining the change and why

## Architecture

See the [Architecture section in README.md](README.md#architecture) for an overview.
The codebase is organized into layers:

- **CLI / Agent SDK** (`cli.py`, `agent_sdk.py`) - User-facing interfaces
- **Repository** (`repo.py`) - High-level operations
- **Managers** (`workspace.py`, `state.py`) - Domain logic
- **Content Store** (`cas.py`) - Content-addressed storage
- **SQLite** - Persistence layer (WAL mode)

## Reporting Issues

Please file issues on GitHub with:
- Steps to reproduce
- Expected vs actual behavior
- Python version and OS
- Fla version (`fla --version`)
