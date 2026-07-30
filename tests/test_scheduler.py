"""Phase 3: the scheduler's "is a target due" logic (pure, time-injected), the
schedule API endpoint, and the _scheduler_tick submission pass."""
import asyncio
import time
from datetime import datetime, timezone

import main
import sync_engine

NOW = 1_000_000.0  # fixed reference epoch for deterministic tests


def _call(coro):
    return asyncio.run(coro)


def _ts(epoch: float) -> str:
    """Render an epoch as the SQLite CURRENT_TIMESTAMP format (naive UTC)."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _target(**over):
    t = {"id": "t", "enabled": 1, "interval_seconds": 3600, "last_run": None}
    t.update(over)
    return t


# --- next_run_at / is_due ---

def test_manual_interval_never_auto_runs():
    t = _target(interval_seconds=0)
    assert sync_engine.next_run_at(t, NOW) is None
    assert sync_engine.is_due(t, NOW) is False


def test_disabled_never_auto_runs():
    t = _target(enabled=0, interval_seconds=3600)
    assert sync_engine.next_run_at(t, NOW) is None
    assert sync_engine.is_due(t, NOW) is False


def test_never_run_is_due_now():
    t = _target(last_run=None)
    assert sync_engine.next_run_at(t, NOW) == NOW
    assert sync_engine.is_due(t, NOW) is True


def test_recent_run_not_due():
    # ran 30 min ago, interval 1h -> next run is 30 min in the future
    t = _target(interval_seconds=3600, last_run=_ts(NOW - 1800))
    assert sync_engine.next_run_at(t, NOW) == NOW + 1800
    assert sync_engine.is_due(t, NOW) is False


def test_elapsed_interval_is_due():
    # ran 2h ago, interval 1h -> overdue
    t = _target(interval_seconds=3600, last_run=_ts(NOW - 7200))
    assert sync_engine.is_due(t, NOW) is True


def test_exact_boundary_is_due():
    # ran exactly one interval ago -> due (next_run == now, <= now)
    t = _target(interval_seconds=3600, last_run=_ts(NOW - 3600))
    assert sync_engine.is_due(t, NOW) is True


def test_unparseable_timestamp_treated_as_due():
    t = _target(last_run="not-a-timestamp")
    assert sync_engine.is_due(t, NOW) is True


# --- due_targets filtering ---

def test_due_targets_filters_the_batch():
    targets = [
        _target(id="due_never", last_run=None),
        _target(id="due_overdue", last_run=_ts(NOW - 7200)),
        _target(id="not_due_recent", last_run=_ts(NOW - 60)),
        _target(id="manual", interval_seconds=0),
        _target(id="disabled", enabled=0),
    ]
    assert sorted(sync_engine.due_targets(targets, NOW)) == ["due_never", "due_overdue"]


def test_interval_presets_exposed():
    assert sync_engine.INTERVAL_PRESETS["manual"] == 0
    assert sync_engine.INTERVAL_PRESETS["daily"] == 86400


# --- schedule API endpoint (drives the real FastAPI coroutines) ---

def test_schedule_endpoint_updates_interval_and_reports_next_run():
    main.db.upsert_target(id="sched_t", name="S", source_path="/s", enabled=True)
    res = _call(main.set_target_schedule("sched_t", main.SchedulePayload(interval_seconds=3600)))
    assert res["status"] == "success"
    assert res["target"]["interval_seconds"] == 3600
    # enabled + interval>0 + never run -> due now -> next_run populated
    assert res["target"]["next_run"] is not None


def test_schedule_endpoint_rejects_negative_interval():
    main.db.upsert_target(id="sched_neg", name="S", source_path="/s", enabled=True)
    res = _call(main.set_target_schedule("sched_neg", main.SchedulePayload(interval_seconds=-5)))
    assert res["status"] == "error"


def test_schedule_endpoint_rejects_sub_minimum_interval():
    main.db.upsert_target(id="sched_fast", name="S", source_path="/s", enabled=True)
    res = _call(main.set_target_schedule("sched_fast", main.SchedulePayload(interval_seconds=30)))
    assert res["status"] == "error"                       # < MIN_INTERVAL_SECONDS rejected
    # 0 (manual) is still allowed
    ok = _call(main.set_target_schedule("sched_fast", main.SchedulePayload(interval_seconds=0)))
    assert ok["status"] == "success"


def test_schedule_endpoint_missing_target():
    res = _call(main.set_target_schedule("nope", main.SchedulePayload(interval_seconds=3600)))
    assert res["status"] == "error"


def test_db_targets_includes_next_run():
    main.db.upsert_target(id="nr_t", name="N", source_path="/s", interval_seconds=0, enabled=True)
    res = _call(main.list_db_targets())
    t = next(x for x in res["targets"] if x["id"] == "nr_t")
    assert "next_run" in t and t["next_run"] is None   # manual -> no next run
    assert t["transferred_today"] == 0                 # counter present, empty


def test_db_targets_counts_todays_transfers():
    main.db.upsert_target(id="cnt_t", name="C", source_path="/s", enabled=True)
    main.db.record_transfers("cnt_t", ["x.mkv", "y.mkv", "z.mkv"])
    res = _call(main.list_db_targets())
    t = next(x for x in res["targets"] if x["id"] == "cnt_t")
    assert t["transferred_today"] == 3


def test_local_midnight_utc_shape():
    # Deterministic shape check: parseable UTC timestamp, seconds are 00, and it
    # is never in the future relative to now.
    from datetime import datetime, timezone
    s = sync_engine.local_midnight_utc(NOW)
    dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    assert dt.timestamp() <= NOW
    assert NOW - dt.timestamp() < 86400 + 1            # within the last day


# --- _scheduler_tick (submission pass) ---

def _tick_env(monkeypatch, role, targets, fixed_now=NOW):
    """Wire _scheduler_tick's globals: pin time, force role, feed a target list,
    and capture submissions instead of actually running syncs on the pool."""
    submitted = []

    def fake_submit(tid, job_type, fn):
        submitted.append(tid)
        return True

    monkeypatch.setattr(main.time, "time", lambda: fixed_now)   # deterministic clock
    monkeypatch.setattr(main, "get_current_role", lambda: role)
    monkeypatch.setattr(main.db, "list_targets", lambda: targets)
    monkeypatch.setattr(main, "_submit_job", fake_submit)
    return submitted


def test_scheduler_tick_submits_only_due_source_targets(monkeypatch):
    targets = [
        {"id": "due_never", "enabled": 1, "interval_seconds": 3600, "last_run": None},
        {"id": "not_due", "enabled": 1, "interval_seconds": 3600, "last_run": _ts(NOW - 60)},
        {"id": "disabled", "enabled": 0, "interval_seconds": 3600, "last_run": None},
    ]
    submitted = _tick_env(monkeypatch, "source", targets)
    result = _call(main._scheduler_tick())
    assert result == ["due_never"]
    assert submitted == ["due_never"]


def test_scheduler_tick_noop_off_source(monkeypatch):
    targets = [{"id": "due_never", "enabled": 1, "interval_seconds": 3600, "last_run": None}]
    submitted = _tick_env(monkeypatch, "client", targets)
    result = _call(main._scheduler_tick())
    assert result == []            # client mode does nothing
    assert submitted == []


# --- CRUD -> DB store wiring (4c-1) ---

def test_add_source_target_lands_in_store_disabled(monkeypatch):
    # Isolate the hash scanner (no real filesystem scan / threads in the test).
    monkeypatch.setattr(main.hash_scanner_daemon, "add_target", lambda *a, **k: None)
    monkeypatch.setattr(main.hash_scanner_daemon, "remove_target", lambda *a, **k: None)

    res = _call(main.add_source_target(main.SourceTargetModel(name="New Lib", source="/data/new/")))
    tid = res["target"]["id"]
    row = main.db.get_target(tid)
    assert row is not None
    assert row["source_path"] == "/data/new/"
    assert row["enabled"] == 0        # unpaired -> disabled, not schedulable
    assert row["server_id"] is None and row["dest_path"] is None

    _call(main.delete_source_target(tid))
    assert main.db.get_target(tid) is None   # delete removes the store row too
