"""Utilidades compartidas entre modulos de test."""
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES_DIR / f"{name}.txt").read_text(encoding="utf-8")
