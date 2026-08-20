---
status: accepted
---

# Persist conversations and immutable answer versions

OpenKB will replace the flat answer-card history with first-class Conversations
owned by one Desktop Knowledge Base. A follow-up may use a bounded window of
recent completed exchanges to interpret the question, but every turn performs
fresh retrieval from Available Knowledge; Interrupted Answers never enter that
context. Grounded Answers and their Answer Evidence remain immutable, while
regeneration creates selectable Answer Versions and only the selected version
continues the Conversation Context. The Evidence Drawer stores and presents
only evidence actually supplied to the answer model. Existing flat grounded
answers are not migrated into Conversations, accepting that old answer history
will not appear in the new conversation experience in exchange for avoiding
invented relationships between unrelated legacy questions.
Each Answer Version also retains a compact Retrieval Trace with the Catalog
Generation, Document PageTree generations, channel results, and degradation
state used to obtain its immutable Answer Evidence. The trace supports
diagnostics and evaluation; rendering an old answer never reruns retrieval or
requires those derived generations to remain stored.
