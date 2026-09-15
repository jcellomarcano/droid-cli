"""Linter de privacidad: tests/fixtures/*.txt (no tests/fixtures/raw/) no debe filtrar PII."""
import subprocess
from pathlib import Path

from tests import anonymize as anon
from tests.helpers import FIXTURES_DIR

MANIFEST_NAME = "MANIFEST.txt"


def _load_manifest(manifest_path: Path) -> dict:
    """Formato: <name>\\t<sha256>\\t<lines>\\t<source> (generado por tests.anonymize.write_manifest);
    lineas de comentario ('#...') se ignoran. Tests que siembran su propio MANIFEST.txt en tmp_path
    pueden seguir usando el formato corto <name>\\t<source>: solo se necesita <name>."""
    entries = {}
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, _, rest = line.partition("\t")
        entries[name] = rest
    return entries


def check_fixtures_directory(dir_path: Path = None) -> dict:
    directory = dir_path or FIXTURES_DIR
    manifest = _load_manifest(directory / MANIFEST_NAME)
    problems = {}
    for name in manifest:
        p = directory / name
        if not p.exists():
            problems[name] = [("missing", "")]
            continue
        text = p.read_text(encoding="utf-8")
        if not text.strip():
            problems[name] = [("empty", "")]
            continue
        violations = anon.find_violations(text)
        if violations:
            problems[name] = violations
    listed = set(manifest)
    on_disk = {p.name for p in directory.glob("*.txt") if p.name != MANIFEST_NAME}
    unlisted = on_disk - listed
    if unlisted:
        problems["__unlisted__"] = [("unlisted", n) for n in sorted(unlisted)]
    return problems


def test_manifest_is_non_empty():
    assert (FIXTURES_DIR / MANIFEST_NAME).exists(), "corre tests/anonymize.py y crea MANIFEST.txt antes de la suite"
    assert _load_manifest(FIXTURES_DIR / MANIFEST_NAME)


def test_clean_fixtures_have_no_violations_and_manifest_matches_disk():
    problems = check_fixtures_directory()
    assert problems == {}


def test_checker_over_tmp_directory_reports_seeded_violation(tmp_path, monkeypatch):
    from tests import anonymize as anon
    anon.configure((("com.private.realapp", "com.example.app"),), ("privateco",))
    monkeypatch.setattr(anon, "FORBIDDEN_SUBSTRINGS", anon.FORBIDDEN_SUBSTRINGS)
    (tmp_path / "leaky.txt").write_text("package com.privateco.app installed\n", encoding="utf-8")
    (tmp_path / MANIFEST_NAME).write_text("leaky.txt\ttest\n", encoding="utf-8")
    monkeypatch.setattr("tests.test_fixtures_clean.FIXTURES_DIR", tmp_path)
    problems = check_fixtures_directory()
    assert "leaky.txt" in problems
    assert ("forbidden_substring", "privateco") in problems["leaky.txt"]
    anon.configure(*anon.load_private_terms())


def test_checker_over_tmp_directory_is_clean_when_no_violations(tmp_path, monkeypatch):
    (tmp_path / "clean.txt").write_text("nothing sensitive, 10.0.2.5 is fine\n", encoding="utf-8")
    (tmp_path / MANIFEST_NAME).write_text("clean.txt\ttest\n", encoding="utf-8")
    monkeypatch.setattr("tests.test_fixtures_clean.FIXTURES_DIR", tmp_path)
    assert check_fixtures_directory() == {}


def test_raw_dir_is_never_tracked_by_git():
    try:
        result = subprocess.run(
            ["git", "ls-files", "tests/fixtures/raw"],
            cwd=Path(__file__).parent.parent,
            capture_output=True, text=True, timeout=15,
        )
    except FileNotFoundError:
        import pytest
        pytest.skip("git no disponible en este entorno")
    assert result.returncode == 0, f"git ls-files fallo (rc={result.returncode}): {result.stderr}"
    assert result.stdout.strip() == ""


def test_gitignore_excludes_raw_dir():
    gitignore = (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")
    assert "tests/fixtures/raw/" in gitignore
