"""Stable failure groupings shared by Desktop import orchestration."""

from openkb.desktop_document_usability import DOCUMENT_IR_FAILURE_CODES

DIRECT_IMPORT_QUARANTINE_CODES = DOCUMENT_IR_FAILURE_CODES | {
    "legacy_office_parse_failed",
    "legacy_office_runtime_unavailable",
    "model_configuration_invalid",
    "model_response_invalid",
}
