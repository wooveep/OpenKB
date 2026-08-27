# Allow one portable local configuration beside the executable

The optional `openkb.local.json` is the sole mutable runtime file allowed beside
the Portable Desktop Package executable so an operator can select application-wide
logging behavior before the Desktop Shell or Python Engine starts and before any
knowledge base is opened. The release carries a read-only example, while the
user-authored file stays outside the release inventory and its hashes; Application
Logs remain in local application state and the file must not contain credentials.
This narrowly supersedes ADR-0018 and ADR-0023 where they require the entire program
directory to remain read-only, preserving that boundary for every other packaged
file in exchange for portable, pre-start diagnostics.
