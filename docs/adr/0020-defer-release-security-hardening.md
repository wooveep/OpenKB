# Defer release security hardening until after the runnable desktop baseline

The first usable Portable Desktop Package prioritizes verified offline startup
and core Desktop Workbench behavior, and may ship unsigned with manual ZIP
replacement. This supersedes the Authenticode requirement in ADR-0015 and
defers update checking, custom image protocols, and advanced Tauri capability
hardening; the baseline still uses one minimal window capability, a scoped
built-in asset protocol, no general shell or filesystem permission, and no
listening service because those are required to keep the selected runtime
operable and bounded rather than a separate security program.
