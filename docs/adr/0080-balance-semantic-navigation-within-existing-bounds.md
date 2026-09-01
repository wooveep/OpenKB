# Balance semantic navigation within existing bounds

OpenKB preserves the model's atomic Retrieval Plan phrases and places them before the
deterministic CJK-bigram fallback. The Retrieval Plan contract explicitly requests separate
semantic concepts and actions; this prevents useful terms such as `双节点`, `超融合`, and
`安装部署` from being reduced to or crowded out by adjacent-character fragments.

Knowledge Navigation keeps its existing limit of four reads and two Source Read Windows. One
relevant source-backed Document Summary is reserved when catalog pages would otherwise consume
all four reads. Source windows are ranked primarily by their original section, then by summary
unit relevance and role. Revision history and tables of contents are deprioritized, as are
expansion, maintenance, recovery, upgrade, or appendix scopes not requested by the query.
Navigation runs after PageTree evidence is known, so its windows supplement the combined
deterministic and PageTree evidence rather than an earlier partial view.

Selected PageTree evidence is round-robined first within each document and then across selected
documents. This prevents one large manual from consuming the bounded supplement before another
selected manual contributes evidence. Fusion continues to protect four deterministic results and
reserve twelve routed results inside the existing 16-reference routed pack. Low-information
fragments are not preferred over substantive protected evidence, and a bounded Source Read
Window replaces a shorter occurrence of the same Evidence ID while retaining all channel labels.

The Grounded Answer contract remains evidence-only for factual authority. For how-to questions it
now requires an actionable synthesis of the available prerequisites, ordered steps, commands or
configuration values, validation, and safety warnings, while marking optional or expansion-only
work. The four-read, two-window, one-PageTree-supplement, 16-reference fusion, and model-capacity
grounding budgets are unchanged.
