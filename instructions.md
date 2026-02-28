# Repository Instructions

This file is the project-level working agreement for Codex and any future coding agent operating in this repository.

## 1. Communication

Before any substantial work, the agent must state the next step clearly.
Each such update must include:
- what will be done
- why it is being done now
- which files are expected to change
- how the result will be verified

The agent should not only report conclusions. It should make the execution plan visible before editing, refactoring, or running broad validation.

If a request spans multiple steps, the agent should keep the user informed at each step boundary instead of batching all reasoning into the final message.

## 2. Commit Discipline

When the work is naturally separable into steps, create one commit per step.
Do not batch framework changes, migrations, and rule implementations into one commit.

Commit messages must follow the repository style:
- `feat (scope): ...`
- `fix (scope): ...`
- `docs (scope): ...`
- `refactor (scope): ...`

Each commit should include a structured message body when the change is non-trivial.
Recommended body sections:
- `why:` why this step exists
- `changes:` concrete modifications in this commit
- `impact:` affected behavior or compatibility notes
- `verify:` validation performed
- `note:` remaining gap or deferred item, if any

A single-line commit message is only acceptable for truly trivial changes.

If the latest local commit only exists to correct a mistake introduced by the agent
in the immediately previous local commit, do not add a new cleanup commit. Amend
or otherwise fold the fix into the previous commit so the history reflects the
intended step cleanly.

## 3. Change Safety

Do not overwrite or revert user changes unless explicitly requested.
If unexpected modifications appear in files relevant to the current task, stop and ask before proceeding.

Unrelated untracked files in the repository should be left alone.

## 4. Repo Instruction File

`instructions.md` is the canonical checked-in document for repo-specific agent instructions.
Do not commit `AGENTS.md` in this repository.

When the user clarifies a new durable working preference, the agent should evaluate whether it belongs here.
If it is a persistent workflow rule rather than a one-off request, update `instructions.md` in the same turn or before the next substantial implementation step.

Examples of rules that should be added here:
- communication format requirements
- commit format requirements
- architecture sequencing preferences
- review or verification expectations

Do not silently rely on repeated conversational memory when a stable project rule should instead be written into this file.
