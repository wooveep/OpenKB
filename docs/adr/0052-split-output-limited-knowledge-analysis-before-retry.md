---
status: accepted
---

# Split output-limited Knowledge Analysis before retry

An explicitly output-limited structured response is incomplete rather than malformed, so it must not consume Structured Output Repair. Knowledge Analysis rejects that physical result, recursively splits its evidence at the nearest natural section boundary inside the same logical plan item, validates each child independently, and deterministically aggregates the children; recursion stops at one Evidence item, where another output-limit result fails normally. The accepted checkpoint records the split leaf and recovery counts, while provider, model, reasoning mode, and the plan's bounded output budget remain pinned.

This supersedes ADR-0042's allowance for a nonempty truncated result to consume Structured Output Repair. Increasing the Knowledge Analysis contract target from 4,096 to 16,384 output tokens reduces avoidable splits for current DeepSeek chat models, but execution profiles cap that reserve at half the configured context so smaller or custom models retain bounded document input.
