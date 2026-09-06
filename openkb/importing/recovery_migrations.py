"""Persist an explicit parser choice with its import recovery attempt."""

IMPORT_PARSER_RECOVERY_MIGRATION_STATEMENTS = (
    "ALTER TABLE recovery_runs ADD COLUMN parser_mode TEXT "
    "CHECK (parser_mode IS NULL OR parser_mode IN ('auto', 'fast', 'enhanced'))",
)
