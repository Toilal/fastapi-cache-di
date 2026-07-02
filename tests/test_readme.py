"""The README's Python examples are held to the project's own standards:
each ```python block must be a valid module that passes ruff and mypy --strict,
using this project's configuration.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parent.parent
_README = _REPO_ROOT / "README.md"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_BIN = Path(sys.executable).parent

_PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _readme_python_blocks() -> list[str]:
    return _PYTHON_BLOCK.findall(_README.read_text(encoding="utf-8"))


def _write_blocks(target: Path) -> list[Path]:
    files = []
    for index, block in enumerate(_readme_python_blocks()):
        path = target / f"readme_example_{index}.py"
        path.write_text(block, encoding="utf-8")
        files.append(path)
    return files


def test_readme_has_examples() -> None:
    assert _readme_python_blocks(), "no ```python examples found in README.md"


def test_readme_examples_pass_ruff(tmp_path: Path) -> None:
    files = _write_blocks(tmp_path)
    result = subprocess.run(
        [str(_BIN / "ruff"), "check", "--config", str(_PYPROJECT), *map(str, files)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_readme_examples_pass_mypy_strict(tmp_path: Path) -> None:
    files = _write_blocks(tmp_path)
    result = subprocess.run(
        [
            str(_BIN / "mypy"),
            "--strict",
            "--config-file",
            str(_PYPROJECT),
            *map(str, files),
        ],
        capture_output=True,
        text=True,
        # Resolve the package's real (typed) source rather than the editable
        # install, so examples are checked against actual types, not Any.
        env={**os.environ, "MYPYPATH": str(_REPO_ROOT / "src")},
    )
    assert result.returncode == 0, result.stdout + result.stderr


if not (_BIN / "ruff").exists() or not (_BIN / "mypy").exists():  # pragma: no cover
    pytest.skip("ruff/mypy not installed in this environment", allow_module_level=True)
