# Accept empty evidence-bound knowledge graphs

Knowledge Graph extraction may succeed with no nodes or edges because absence
of evidence-backed relationships is a valid domain result. Its Prompt Contract
still uses a nonempty, fully evidence-bound canonical example so validation is
exercised without inventing graph content merely to satisfy the example; the
result publishes a `completed_empty` generation that replaces older graph
relationships for that source and is reported as a normal zero-count success.
