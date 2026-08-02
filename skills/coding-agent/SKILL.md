---
name: coding-agent
description: Assist with code editing, linting, testing, and safe patch generation. Use when the agent needs to propose or apply code changes, run linters/tests, or produce clear diffs for human review.
user-invocable: true
---

# coding-agent

Purpose
- Small utility skill that helps the assistant edit code reliably and safely by providing a scaffolded set of scripts and a clear, minimal workflow.

When to use
- Use this skill when you want the agent to:
  - Propose code changes and generate unified diffs (patches).
  - Run linters or test commands and return results.
  - Produce small, reviewable edits to repository files and present them for human approval before applying.

Files included
- scripts/patch_helper.mjs — helper to create JSON patches or unified diffs (for human approval).
- scripts/lint.sh — runs project linter(s) if available (safe: checks existence before running).
- scripts/run_tests.sh — runs test runner if present (safe: checks before running).

Security & behavior
- This skill only *proposes* edits. It will never modify files or restart services without explicit user approval.
- When used, the agent will:
  1. Read files that are in the workspace (as-needed).
  2. Generate a small patch or script output.
  3. Present the diff and ask for explicit approval (/approve) before applying any changes.

Quick developer notes
- Keep the SKILL.md short. Put heavyweight references or examples into scripts/ or references/ if needed.

