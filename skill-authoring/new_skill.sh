#!/bin/bash
# new_skill.sh — scaffold a new OpenClaw workspace skill from the template.
# usage: ./new_skill.sh <skill-name> ["one-line description of when to use it"]
set -e
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
SKILLS_DIR="$HOME/.openclaw/workspace/skills"
NAME="$1"; DESC="${2:-TODO: describe precisely WHEN to use this skill}"
[ -z "$NAME" ] && { echo "usage: $0 <skill-name> [\"description\"]" >&2; exit 1; }
DIR="$SKILLS_DIR/$NAME"
[ -e "$DIR" ] && { echo "error: $DIR already exists" >&2; exit 1; }
mkdir -p "$DIR"
sed -e "s/{{NAME}}/$NAME/g" -e "s|{{DESCRIPTION}}|$DESC|g" \
    "$SELF_DIR/TEMPLATE.SKILL.md" > "$DIR/SKILL.md"
echo "created $DIR/SKILL.md  — edit it, then it auto-registers."
openclaw skills info "$NAME" 2>&1 | head -10 || true
