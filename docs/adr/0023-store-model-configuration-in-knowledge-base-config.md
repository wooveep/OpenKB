# Store model configuration in the knowledge-base configuration

This decision supersedes only the credential-storage portion of ADR-0018. At
the user's express choice, each Desktop Knowledge Base stores its API Base URL,
API Key, and model directly in `.openkb/config.yaml`, entered through the
Desktop Workbench and never sourced from environment variables. The API Key is
plain text in that file, trading the prior OS credential protection for a
portable and directly editable per-knowledge-base configuration; the program
directory remains read-only, Application Logs stay in local application state,
and Diagnostic Bundles continue to exclude credentials.
Knowledge extraction, PageTree Enrichment, PageTree Selection, and answer
generation reuse this Model Configuration and the same gateway, diagnostics,
and provider routing; separate per-capability model settings remain deferred.
