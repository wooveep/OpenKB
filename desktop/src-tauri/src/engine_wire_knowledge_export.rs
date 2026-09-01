//! Wire-safe Knowledge Bundle export and preview values.

use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KnowledgeExportMode {
    KnowledgeProjection,
    SelfContained,
    PortableWiki,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeExportResult {
    pub path: String,
    pub mode: KnowledgeExportMode,
    pub files: Vec<String>,
    #[serde(alias = "raw_asset_count")]
    pub raw_asset_count: u64,
    #[serde(alias = "source_image_count")]
    pub source_image_count: u64,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KnowledgeExportPreview {
    pub mode: KnowledgeExportMode,
    #[serde(alias = "document_count")]
    pub document_count: u64,
    #[serde(alias = "estimated_size_bytes")]
    pub estimated_size_bytes: u64,
    #[serde(alias = "snapshot_id")]
    pub snapshot_id: String,
}

#[cfg(test)]
mod tests {
    use super::{KnowledgeExportMode, KnowledgeExportPreview, KnowledgeExportResult};
    use serde_json::json;

    #[test]
    fn knowledge_export_accepts_python_snake_case_counts() {
        let export: KnowledgeExportResult = serde_json::from_value(json!({
            "path": "C:/Exports/OpenKB-Portable-Wiki",
            "mode": "portable_wiki",
            "files": ["index.md", "wiki-manifest.json"],
            "raw_asset_count": 1,
            "source_image_count": 2
        }))
        .expect("Python Knowledge export payload should deserialize");

        assert_eq!(export.raw_asset_count, 1);
        assert_eq!(export.source_image_count, 2);
        assert!(matches!(export.mode, KnowledgeExportMode::PortableWiki));
    }

    #[test]
    fn knowledge_export_preview_accepts_python_snake_case_counts() {
        let preview: KnowledgeExportPreview = serde_json::from_value(json!({
            "mode": "portable_wiki",
            "document_count": 7,
            "estimated_size_bytes": 8192,
            "snapshot_id": "snapshot-7"
        }))
        .expect("Python Knowledge export preview should deserialize");

        assert_eq!(preview.document_count, 7);
        assert_eq!(preview.estimated_size_bytes, 8192);
        assert_eq!(preview.snapshot_id, "snapshot-7");
    }
}
