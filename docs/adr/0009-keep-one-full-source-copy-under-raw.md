# Keep one full source copy under raw

OpenKB retains each completed import's full source bytes only once under
`raw/`; SQLite stores its hash, length, identity, and lifecycle metadata, while
CAS is reserved for large derived artifacts rather than a second full-source
copy. SQLite and `raw/` therefore form one backup and restore unit, and a
missing or corrupt Raw Asset quarantines the document because the application
cannot reconstruct it locally.
