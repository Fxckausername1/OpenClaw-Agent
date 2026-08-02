# Authoring OpenClaw skills

A skill is a directory containing a `SKILL.md`. The model reads the frontmatter
`description` to decide when to auto-invoke it.

## Where skills come from (sources)
- `openclaw-bundled` — ship with OpenClaw.
- `openclaw-extra`   — provided by installed plugins.
- `openclaw-workspace` — YOURS, authored here: ~/.openclaw/workspace/skills/<name>/SKILL.md
- ClawHub registry — `openclaw skills search|install|update` pulls community skills.

## Create one
    cd ~/.openclaw/workspace/skill-authoring
    ./new_skill.sh my-skill "Use when ... (be specific about WHEN)"
    # then edit ~/.openclaw/workspace/skills/my-skill/SKILL.md

Or by hand: make the dir + SKILL.md, copying TEMPLATE.SKILL.md.

## Frontmatter
    ---
    name: my-skill                 # kebab-case, matches the directory name
    description: Use when ...       # what the model matches on — be specific
    user-invocable: true            # true = callable by name; false = model-only helper
    ---

## Verify / manage
    openclaw skills list            # all skills + ready/needs-setup status
    openclaw skills info my-skill   # path, visibility, command availability
    openclaw skills check           # what is ready vs missing requirements

## Tips
- The `description` is the single most important field — it drives auto-invocation.
- Keep the body action-oriented: a "When to use" section + numbered steps.
- Bundle helper scripts/data in the skill dir; reference by relative path.
