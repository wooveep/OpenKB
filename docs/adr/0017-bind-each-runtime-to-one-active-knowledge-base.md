# Bind each Desktop Runtime to one Active Knowledge Base

The Desktop Runtime is globally single-instance and binds its persistent
Python Engine to exactly one Active Knowledge Base at a time; a second launch
wakes the existing window and forwards open or import intent instead of
creating another writer. Knowledge-base switching checkpoints current work and
rebinds the runtime, favoring simple write ownership and recovery over
simultaneous multi-knowledge-base windows in the first release.
