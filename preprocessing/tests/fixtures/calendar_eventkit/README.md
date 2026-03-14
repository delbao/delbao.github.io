These are persisted EventKit snapshot fixtures for fixed historical windows.

Purpose:
- keep integration tests deterministic
- validate matching behavior against real calendar data captured from EventKit
- allow optional live parity checks

Refresh fixtures from local Calendar using Swift/EventKit:

```bash
cd /Users/dbao/my/delbao.github.io
PYTHONPATH=/Users/dbao/my/delbao.github.io/preprocessing \
  ./.venv/bin/python preprocessing/tests/record_calendar_eventkit_snapshots.py
```

Run snapshot integration tests (no live Calendar access required):

```bash
cd /Users/dbao/my/delbao.github.io
PYTHONPATH=/Users/dbao/my/delbao.github.io/preprocessing \
  ./.venv/bin/python -m unittest preprocessing.tests.test_calendar_eventkit_snapshot_integration -v
```

Run optional live EventKit parity check against persisted snapshots:

```bash
cd /Users/dbao/my/delbao.github.io
RUN_CALENDAR_INTEGRATION=1 \
PYTHONPATH=/Users/dbao/my/delbao.github.io/preprocessing \
  ./.venv/bin/python -m unittest preprocessing.tests.test_calendar_eventkit_snapshot_integration -v
```
