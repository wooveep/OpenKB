# Publish completed documents independently of an import batch

An Import Batch is a progress queue rather than an all-or-nothing publication
boundary: each successfully completed document becomes Available Knowledge for
Grounded Answers immediately, while active and quarantined documents remain
excluded. The Desktop Workbench does not show a global incompleteness warning,
because knowledge is expected to evolve continuously; this favors useful early
answers over a batch-consistent snapshot.
