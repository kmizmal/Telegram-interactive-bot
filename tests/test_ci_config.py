from pathlib import Path

CI = Path(__file__).resolve().parents[1] / ".github/workflows/ci.yml"


def test_ci_runs_lock_check_and_pytest():
    content = CI.read_text(encoding="utf-8")

    assert "uv lock --check" in content
    assert "pytest -q" in content
    assert "uv sync --frozen" in content
