# Require source-backed Knowledge Page claims for answers

Knowledge Pages may contain user-authored or generated material, but only a
paragraph, list item, table row, or other claim-level unit whose OKF source
marker resolves to an EvidenceRef in Available Knowledge may contribute content
to a Grounded Answer. Unsourced text remains editable and may assist human
browsing, but it cannot become answer context or gain authority merely because
a user saved the page; this preserves OpenKB's raw source citation contract at
the cost of not treating private, undocumented notes as answerable knowledge.

Knowledge Verification is an explicit human action bound to one complete User
Revision and becomes invalid after any content, source, or lifecycle change.
Draft knowledge is excluded from answers, stable knowledge participates
normally, deprecated knowledge is excluded from default routing, and stale
knowledge is down-ranked without changing the availability of its underlying
EvidenceRefs.

For each revision or published generation, authoritative Markdown in SQLite
uses OKF footnote markers and a normalized Knowledge Source Map resolves those
source identifiers to EvidenceRefs. Both representations are validated and
written in the same transaction; filesystem Markdown remains a rebuildable
projection. This preserves the existing Markdown editing model while avoiding
document-level provenance that would falsely support every statement on a
page.

The editor provides Knowledge Source Binding through document and section
search instead of exposing Evidence IDs. A revision with unresolved source
markers may be saved as a Draft Revision with actionable diagnostics, but it
cannot become stable or verified. If previously valid evidence later becomes
unavailable, the historical revision remains unchanged while the Publication
Gate temporarily excludes its affected claims; eligibility returns if the
source becomes available again.

Source-backed model output may be published as stable but unverified knowledge.
A generated claim without resolvable evidence becomes a Missing Source
Candidate in the Review Queue rather than published knowledge or a reason to
quarantine its source document; binding evidence may promote it, while
dismissal deletes its candidate content.

Source-backed Knowledge Claims are used only to plan retrieval, expand
candidates, and rank their mapped EvidenceRefs. Their rewritten text never
enters the answer model as evidence; the EvidencePack remains source-only. A
claim may cite multiple EvidenceRefs, but canonical D2 duplicates count as one
support and do not inflate rank. Missing Source Candidates share the existing
Review Queue with an explicit category; source binding is individual, while
dismissal may be applied in bulk.

Each page derives a stable Knowledge Source ID from the canonical EvidenceRef,
so source reordering and later revisions do not change footnote identity or
silently redirect a claim. An available occurrence is chosen only when the
source is resolved for retrieval and citation.

Knowledge Trust Tier is only a light Catalog-routing tie-breaker and cannot
override evidence relevance, availability, or protected baseline candidates.
Human review requires every factual claim unit in the published revision to
pass the Publication Gate; headings, navigation, and purely structural prose
are exempt. The first release records human verification only and does not
invent machine-confirmed status from successful Model Calls.
