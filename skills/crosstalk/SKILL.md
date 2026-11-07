---
name: crosstalk
description: Coordinate with other Claude Code sessions running in parallel on the same project. Use when crosstalk context mentions other live sessions, when a file you are about to change is flagged as being edited by another session, or when you make a decision other sessions should know about (a renamed API, a new convention, a migration, "don't touch X").
---

# Working alongside other sessions

crosstalk gives every Claude Code session in a project a shared, local memory.
Hooks already tell you which other sessions are live, what they are working
on, and what they edited since your last turn. Your job is to use that.

## Do
- **Broadcast decisions that affect others**, in one line, right after you make them:
  `python3 "$CLAUDE_PLUGIN_ROOT/scripts/crosstalk.py" note '<decision>'`
  (if `$CLAUDE_PLUGIN_ROOT` is empty in Bash, use the path shown in the session-start context).
  Good notes: "renamed getUser() to fetchUser() in src/api", "auth tests are flaky, skip for now",
  "I own src/db/*, please don't edit until I'm done".
- **Re-read a file before editing it** when crosstalk says another session touched it.
- **Tell the user about overlaps** instead of silently overwriting another session's work.

## Don't
- Don't broadcast routine progress; edits are logged automatically.
- Don't put secrets in notes. They are stored in plain text under `~/.crosstalk/`.
