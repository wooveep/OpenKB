import json

import pytest


@pytest.fixture
def kb_dir(tmp_path):
    """Create a minimal knowledge base directory structure for testing."""
    # Raw documents
    (tmp_path / "raw").mkdir()

    # Wiki sub-directories
    (tmp_path / "wiki" / "sources" / "images").mkdir(parents=True)
    (tmp_path / "wiki" / "summaries").mkdir(parents=True)
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "wiki" / "explorations").mkdir(parents=True)
    (tmp_path / "wiki" / "reports").mkdir(parents=True)

    # .openkb state directory
    openkb_dir = tmp_path / ".openkb"
    openkb_dir.mkdir()

    config_yaml = """\
version: "0.1.0"
embedding_model: text-embedding-3-small
llm_model: gpt-4o-mini
chunk_size: 512
chunk_overlap: 64
"""
    (openkb_dir / "config.yaml").write_text(config_yaml)
    (openkb_dir / "hashes.json").write_text(json.dumps({}))

    return tmp_path
