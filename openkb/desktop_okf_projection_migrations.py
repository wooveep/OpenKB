"""Schema seam for canonical OKF Entity subtype projection."""

OKF_PROJECTION_MIGRATION_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE knowledge_generation_items
    ADD COLUMN entity_subtype TEXT
    """,
)
