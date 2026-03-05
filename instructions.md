## 1. Local Tooling Layout

Python preprocessing and transcript-processing code for this repository should
live under a root-level `preprocessing/` directory.

Repository-local Python virtual environments should live in `.venv/` at the
repository root and stay untracked.

## 2. Meeting Metadata Source

For generated meeting posts, metadata fields shown in the UI (description,
attendees, attachments/links, and source) must come from the original calendar
invite metadata only. Do not generate or rewrite these metadata fields from
transcript content.
