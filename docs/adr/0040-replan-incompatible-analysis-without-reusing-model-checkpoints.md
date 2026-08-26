# Replan incompatible analysis without reusing model checkpoints

Explicit recovery of a legacy or incompatible Knowledge Analysis Plan creates
a replacement plan inside the same Import Job. OpenKB retains the Raw Asset,
DocumentIR, Evidence, and other compatible deterministic work, but it does not
reuse Knowledge Analysis Batch or merge checkpoints from the superseded plan.
This may repeat paid model work, but it prevents one published result from
mixing outputs produced under different Model Execution Profiles; reimporting
and reparsing the source would repeat unrelated deterministic work.

The Task Drawer presents the failure summary and opens the existing Failed
Documents recovery surface. That surface shows the discarded checkpoints and
estimated replacement calls before the user confirms a one-time Recovery
Override such as “reasoning off and Replan”; it also links to Model
Configuration for users who want to change future imports. Recovery never
silently modifies knowledge-base defaults or starts a model call.

The recommended recovery action is explicitly labeled “check and recover.” One
confirmation authorizes a bounded Model Capability Check for the one-time
profile and, only after it succeeds, the already-estimated Knowledge Analysis
Replan. A failed check leaves the document quarantined and starts no analysis
batches.
