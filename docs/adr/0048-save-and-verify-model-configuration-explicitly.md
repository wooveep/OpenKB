# Save and verify model configuration explicitly

Model configuration readiness uses one explicit Save and Verify action that
persists the configuration and then runs the required role-specific capability
checks after disclosing possible provider cost. This retains informed consent
for every verification call while avoiding a saved-but-silently-unverified
configuration as the normal path; failed or cancelled checks do not roll back
the saved configuration, and each role retains its own result unless its full
role and check signature is identical to a reusable result. Analysis and Answer
are the required runtime roles. A distinct Default value is reported as
`not_required` and does not trigger a third paid check; when Default inherits
Answer, its result is explicitly reported as covered by Answer. Stop is checked
between role dispatches: it prevents checks not yet sent, but does not interrupt
an already-dispatched check, whose completed evidence is retained.
