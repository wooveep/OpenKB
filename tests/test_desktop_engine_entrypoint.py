"""Frozen Engine startup must not load a second desktop_engine module identity."""

from pathlib import Path

from openkb import desktop_engine, desktop_engine_entrypoint


def test_portable_engine_freezes_a_canonical_import_wrapper() -> None:
    assert desktop_engine_entrypoint.main is desktop_engine.main
    package_script = (
        Path(__file__).parents[1] / "desktop" / "scripts" / "New-PortablePackage.ps1"
    ).read_text(encoding="utf-8")
    assert 'openkb\\desktop_engine_entrypoint.py"' in package_script
    assert 'openkb\\desktop_engine.py"' not in package_script
