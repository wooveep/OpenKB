# Reconcile reimports with user-reviewed conflicts

Every reimport first runs duplicate detection, then reconciles matching
concepts and entities as duplicate, additive, or conflicting. Conflicting
updates enter the Review Queue for an individual user decision or a bulk
version choice, rather than silently overwriting the current or user-revised
knowledge page. This applies equally to a newly imported source and a new
version of a previously imported source.
When the affected Knowledge Page has a Working Draft, reconciliation uses the
Current Published Revision as its stable baseline and presents published,
draft, and incoming content together; no compatible or conflicting incoming
change may overwrite the unpublished draft automatically.
The resolution may retain the draft, apply incoming changes to it, replace it,
or support a manual merge, but the result remains a Working Draft until the
user explicitly publishes it.
