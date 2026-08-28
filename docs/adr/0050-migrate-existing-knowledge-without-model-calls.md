# Migrate existing knowledge without model calls

Knowledge Workspace schema upgrades are additive, transactional, and preceded
by a restorable authority-data backup; they expose the current generated
generation in place without reanalysis or Markdown rewriting. Opening an
unchanged valid OKF projection preserves its bytes and file timestamps instead
of replacing the tree. Backups use a unique attempt identity and retain a
bounded number per schema-version edge. Legacy capability migration carries
only durable exact evidence that is still recorded as verified; unchecked,
invalidated, or causally ambiguous history remains unverified. Migration never
invokes a provider.
