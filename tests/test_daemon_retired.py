"""Phase 4a: lsyncd is retired — the daemon controller is inert and the daemon
log-tailer no longer runs (the DB scheduler drives replication; per-target log
buckets are fed directly by the sync engine)."""
import main


def test_controller_fully_removed():
    # Phase 4b deleted docker_controller entirely — the app wires no lsyncd
    # process manager anymore.
    assert not hasattr(main, "docker_ctrl")


def test_health_reports_retired():
    import asyncio
    body = asyncio.run(main.health_check())
    assert body["daemon_status"] == "retired"


def test_start_tailing_is_noop():
    # Called from a SYNC context with no running event loop. The old start_tailing
    # called asyncio.create_task and would raise "no running event loop" here; the
    # retired no-op returns None cleanly, proving it starts no background tailer.
    assert main.log_router.start_tailing() is None


def test_log_buckets_still_work():
    # The SSE bucket machinery the sync engine writes to must remain functional.
    main.log_router._ensure_bucket_exists("retire_probe")
    main.log_router._add_log("retire_probe", "hello")
    assert any("hello" in line for line in main.log_router.target_logs["retire_probe"])
