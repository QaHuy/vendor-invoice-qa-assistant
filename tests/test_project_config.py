import importlib
import tomllib
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _gitignore_lines() -> set[str]:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    return {line.strip() for line in text.splitlines() if line.strip()}


def test_package_imports():
    module = importlib.import_module("invoice_qa")
    assert module.__name__ == "invoice_qa"


@pytest.mark.parametrize(
    "package",
    [
        "invoice_qa.domain",
        "invoice_qa.extraction",
        "invoice_qa.infrastructure",
        "invoice_qa.services",
        "invoice_qa.ui",
    ],
)
def test_subpackages_import(package):
    importlib.import_module(package)


@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        ".data/",
        "*.sqlite3",
        "*.db",
        ".streamlit/secrets.toml",
        "*.log",
        "uploads/",
        "*.tmp",
        "__pycache__/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".venv/",
        ".coverage",
        "htmlcov/",
        "sample_data/generated/",
        ".evaluation/",
    ],
)
def test_gitignore_excludes(pattern):
    assert pattern in _gitignore_lines()


def test_env_example_has_safe_defaults_only():
    lines = [
        line.strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == ["INVOICE_QA_DB_PATH=.data/invoice_qa.sqlite3"]


def test_config_default_db_path(monkeypatch):
    from invoice_qa import config

    monkeypatch.delenv("INVOICE_QA_DB_PATH", raising=False)
    assert config.get_db_path() == Path(".data/invoice_qa.sqlite3")


def test_config_db_path_from_environment(monkeypatch, tmp_path):
    from invoice_qa import config

    target = tmp_path / "custom.sqlite3"
    monkeypatch.setenv("INVOICE_QA_DB_PATH", str(target))
    assert config.get_db_path() == target


def test_default_clock_returns_a_date():
    from invoice_qa import config

    assert isinstance(config.default_today(), date)


def test_streamlit_upload_limit_is_5_mb():
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["maxUploadSize"] == 5


def test_requires_python_311_and_ruff_target():
    data = _pyproject()
    assert data["project"]["requires-python"] == ">=3.11"
    assert data["tool"]["ruff"]["target-version"] == "py311"


def test_pytest_configuration():
    options = _pyproject()["tool"]["pytest"]["ini_options"]
    assert options["pythonpath"] == ["src"]
    assert options["testpaths"] == ["tests"]


def _names(requirements: list[str]) -> set[str]:
    names = set()
    for requirement in requirements:
        name = requirement
        for separator in ("==", ">=", "<=", "~=", "<", ">", "[", ";", " "):
            name = name.split(separator)[0]
        names.add(name.strip().lower())
    return names


def test_runtime_dependencies_are_exactly_the_documented_set():
    dependencies = _pyproject()["project"]["dependencies"]
    assert _names(dependencies) == {
        "streamlit",
        "pypdf",
        "pydantic",
        "pandas",
        "reportlab",
    }


def test_streamlit_cloud_requirements_install_the_project():
    # Streamlit Community Cloud reads pyproject.toml as Poetry only, so requirements.txt
    # must install this package (and through it the pyproject dependencies) instead.
    lines = [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines == ["."]


def test_reportlab_is_pinned_exactly():
    dependencies = _pyproject()["project"]["dependencies"]
    reportlab = [d for d in dependencies if d.lower().startswith("reportlab")]
    assert len(reportlab) == 1 and "==" in reportlab[0]


def test_dev_dependencies_are_pytest_and_ruff():
    dev = _pyproject()["project"]["optional-dependencies"]["dev"]
    assert _names(dev) == {"pytest", "ruff"}


def test_no_real_model_sdk_or_undeclared_pdf_library():
    forbidden = {
        "openai",
        "anthropic",
        "google-generativeai",
        "pymupdf",
        "fitz",
        "pdfminer.six",
        "pdfplumber",
        "fpdf",
        "fpdf2",
        "requests",
        "httpx",
    }
    data = _pyproject()
    declared = _names(data["project"]["dependencies"]) | _names(
        data["project"]["optional-dependencies"]["dev"]
    )
    assert not declared & forbidden
