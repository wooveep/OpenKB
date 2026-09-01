"""Knowledge-base language defaults remain stable across mixed documents."""

from __future__ import annotations

import threading

import openkb.config as config_module
from openkb.config import (
    ensure_preferred_knowledge_language,
    load_config_mapping,
    preferred_knowledge_language,
    save_config,
)
from openkb.locks import kb_ingest_lock, kb_ingest_lock_held


def test_dominant_language_is_inferred_once_and_persisted(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    config_path = kb_dir / ".openkb" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    save_config(config_path, {"model": "test-model"})

    language = ensure_preferred_knowledge_language(
        kb_dir,
        ("双节点超融合安装部署。", "配置网络并检查服务。", "OCloud View"),
    )

    assert language == "zh"
    assert preferred_knowledge_language(kb_dir) == "zh"
    assert load_config_mapping(config_path)["knowledge"] == {
        "language": "zh",
        "language_origin": "corpus_default",
    }
    assert ensure_preferred_knowledge_language(kb_dir, ("English-only later document.",)) == "zh"


def test_explicit_language_override_is_never_replaced_by_inference(tmp_path) -> None:
    kb_dir = tmp_path / "knowledge"
    config_path = kb_dir / ".openkb" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    save_config(config_path, {"knowledge": {"language": "en"}})

    language = ensure_preferred_knowledge_language(
        kb_dir,
        ("双节点超融合安装部署。", "配置网络并检查服务。"),
    )

    assert language == "en"
    assert load_config_mapping(config_path)["knowledge"] == {"language": "en"}


def test_language_inference_preserves_a_concurrent_settings_write(tmp_path, monkeypatch) -> None:
    kb_dir = tmp_path / "knowledge"
    state_dir = kb_dir / ".openkb"
    config_path = state_dir / "config.yaml"
    state_dir.mkdir(parents=True)
    save_config(config_path, {"model": "old-model"})

    language_read = threading.Event()
    settings_attempted = threading.Event()
    settings_saved = threading.Event()
    failures: list[BaseException] = []
    original_load = config_module.load_config_mapping
    original_save = config_module.save_config

    def observed_load(path):
        snapshot = original_load(path)
        if threading.current_thread().name == "language-inference":
            language_read.set()
            assert settings_attempted.wait(timeout=2)
            if not kb_ingest_lock_held(state_dir):
                assert settings_saved.wait(timeout=2)
        return snapshot

    monkeypatch.setattr(config_module, "load_config_mapping", observed_load)

    def infer_language() -> None:
        try:
            ensure_preferred_knowledge_language(kb_dir, ("双节点超融合安装部署。",))
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    def save_settings() -> None:
        try:
            assert language_read.wait(timeout=2)
            settings_attempted.set()
            with kb_ingest_lock(state_dir):
                config = original_load(config_path)
                config["model"] = "new-model"
                config["desktop"] = {"api_key": "new-secret"}
                original_save(config_path, config)
            settings_saved.set()
        except BaseException as error:  # pragma: no cover - asserted below
            failures.append(error)

    language_thread = threading.Thread(target=infer_language, name="language-inference")
    settings_thread = threading.Thread(target=save_settings, name="settings-write")
    language_thread.start()
    settings_thread.start()
    language_thread.join(timeout=3)
    settings_thread.join(timeout=3)

    assert not language_thread.is_alive()
    assert not settings_thread.is_alive()
    assert failures == []
    config = load_config_mapping(config_path)
    assert config["model"] == "new-model"
    assert config["desktop"] == {"api_key": "new-secret"}
    assert config["knowledge"]["language"] == "zh"
