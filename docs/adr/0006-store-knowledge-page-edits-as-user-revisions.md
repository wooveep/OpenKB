# Store knowledge-page edits as user revisions

Desktop edits to concept and entity Knowledge Pages create User Revisions in
SQLite and then rematerialize Markdown from that authority, rather than
modifying Markdown directly. This preserves a single source of truth while
keeping the human-readable wiki view synchronized with user decisions. A
Knowledge Page may hold a recoverable Working Draft alongside one Current
Published Revision; saving the draft does not displace published knowledge,
and publication atomically advances the current revision only after the draft
passes the Publication Gate and the user explicitly chooses Publish. Autosave
never publishes.
The Knowledge editor keeps these actions together: autosave updates the
Working Draft, Publish advances the Current Published Revision, explicit
verification marks an already published revision, and deprecation or permanent
deletion live behind secondary actions rather than separate workspace pages.
