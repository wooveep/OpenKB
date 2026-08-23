# Recover legacy model deadlines without automatic calls

Upgrading does not automatically rerun imports that ended under the former
`model_deadline_exceeded` policy. The task surface offers two explicit recovery
actions: continue compatible completed Knowledge Analysis Batches under the new
no-response-deadline behavior, or restart Knowledge Analysis with a new dynamic
Knowledge Analysis Plan. It estimates the remaining calls and input volume for
both choices and recommends the lower projected cost without starting it.

A legacy checkpoint is compatible only when its stored prompt digest matches a
Prompt Contract still known to the application. OpenKB may synthesize a pinned
legacy plan in that case. An unknown digest cannot be mixed with current output
and requires a fresh Knowledge Analysis Plan. Published historical analysis is
unchanged; this migration applies only to incomplete or recovery-required work.
