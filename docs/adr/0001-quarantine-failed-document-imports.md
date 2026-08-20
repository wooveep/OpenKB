# Quarantine imports after unrecoverable model failures

If a required Model Call exhausts its 60-second response deadline or fails its
initial attempt plus up to three retries, the document is quarantined and none
of its partial results may appear in Grounded Answers until a user manually
recovers it. The default first-attempt response timeout is 20 seconds; each
retry increases that timeout by 10 seconds, but the 60-second logical deadline
always wins. Authentication, configuration, and response-format failures
quarantine immediately without retry; this deliberately favors answer
integrity and a persistent recovery path over exposing incomplete knowledge.
