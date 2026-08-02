---
name: {{NAME}}
description: {{DESCRIPTION}}
user-invocable: true
---

# {{NAME}}

## When to use
Describe the trigger precisely. The `description` field above is what the model
matches on to auto-invoke this skill, so be specific about *when* to use it, not
just what it does.

## Steps
1. ...
2. ...

## Notes
- Set `user-invocable: false` for a background/helper skill the model should use
  but the user shouldn't call by name.
- Put supporting files (scripts, reference docs) in this directory and reference
  them by relative path from this SKILL.md.
