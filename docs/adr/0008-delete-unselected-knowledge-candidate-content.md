# Delete unselected knowledge-candidate content after submission

Individual and batch Conflict choices remain staged until the user submits
them. Submission physically deletes the unselected derived concept or entity
candidate content, while retaining source documents, Document Versions,
EvidenceRefs, and a minimal Resolution Record. This keeps the chosen knowledge
surface small but means recovering discarded candidate text requires running
extraction again.
