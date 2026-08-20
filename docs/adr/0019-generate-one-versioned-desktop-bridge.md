# Generate one versioned Desktop Bridge across the runtime boundary

The React workbench depends on one replaceable Desktop Bridge rather than
calling Tauri commands or constructing Engine messages from components.
Versioned Python request, response, error, and event models define the contract
and generate TypeScript and Rust types, while a revisioned snapshot plus
sequenced events drives the Workbench Store; this adds schema generation but
prevents three runtimes from drifting and creates one high-level test seam.
