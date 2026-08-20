# Resume imports with run-scoped recovery overrides

Manual recovery resumes at the failed Stage Run and reuses the validated
outputs of earlier stages. Any model or timeout settings selected for that
recovery are captured as a Recovery Override for that run only, rather than
changing the knowledge base defaults; this preserves successful work and makes
troubleshooting reproducible without surprising later imports.
