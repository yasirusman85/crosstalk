---
description: Broadcast a decision or warning to every other Claude Code session in this project
argument-hint: <one-line note>
allowed-tools: Bash(python3:*)
---
Share this note with the other sessions by running exactly one Bash command:

python3 "${CLAUDE_PLUGIN_ROOT}/scripts/crosstalk.py" note '<the note>'

The note is: $ARGUMENTS

Put the note in single quotes (escape any single quote inside it). Then confirm in one line that it was shared.
