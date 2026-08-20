# Build the Desktop Workbench with Tauri, React, and a Python Engine

The Desktop Shell uses Tauri 2 with a React/Vite workbench and supervises a
packaged Python Engine child process. Tauri and Rust own native Windows
integration, secure IPC, and process lifecycle while all OpenKB application and
domain behavior remains in Python; this replaces CustomTkinter in favor of a
richer, faster-evolving UI and reuse of the existing frontend ecosystem, while
accepting a Rust build toolchain and a larger self-contained package.
