"""Release evidence cannot survive production edits or omitted smoke scenarios."""

import shutil

import pytest
from test_semantic_quality_evaluation import REPOSITORY_ROOT

from evaluation.semantic_quality.implementation import _FILES, _TREES, implementation_digest


@pytest.fixture
def implementation_copy(tmp_path):
    for name in _TREES:
        shutil.copytree(REPOSITORY_ROOT / name, tmp_path / name)
    for name in _FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY_ROOT / name, target)
    return tmp_path


@pytest.mark.parametrize(
    "name",
    [
        "openkb/models/transport.py",
        "openkb/answers/grounded.py",
        "frontend/src/main.tsx",
        "desktop/src-tauri/src/main.rs",
        "uv.lock",
        "desktop/scripts/New-PortablePackage.ps1",
    ],
)
def test_release_digest_changes_for_every_production_boundary(implementation_copy, name):
    root = implementation_copy
    original = implementation_digest(root)
    path = root / name
    path.write_bytes(path.read_bytes() + b"\n")
    assert implementation_digest(root) != original


def test_release_digest_covers_added_and_deleted_modules_but_ignores_secrets(implementation_copy):
    root = implementation_copy
    original = implementation_digest(root)
    (root / ".env").write_text("SECRET=never-part-of-a-release-binding")
    assert implementation_digest(root) == original
    added = root / "openkb/new_module.py"
    added.write_text("value = 1\n")
    assert implementation_digest(root) != original
    added.unlink()
    assert implementation_digest(root) == original
    (root / "openkb/models/transport.py").unlink()
    assert implementation_digest(root) != original
