import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.storage.database import Database
from app.storage.usage import BudgetExceeded, UsageStore


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "usage.db"))
    await database.open()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_atomic_group_and_global_budgets_count_all_pending_attempts(db):
    usage = UsageStore(db, global_daily_limit=5, default_chat_daily_limit=3)

    async def attempt(chat_id):
        try:
            return await usage.reserve(chat_id, "decision", "model")
        except BudgetExceeded:
            return None

    first = await asyncio.gather(*(attempt(-100) for _ in range(20)))
    assert sum(item is not None for item in first) == 3
    second = await asyncio.gather(*(attempt(-200) for _ in range(20)))
    assert sum(item is not None for item in second) == 2
    snapshot = await usage.snapshot(None)
    assert snapshot["calls"] == 5
    assert snapshot["decision_calls"] == 5
    assert snapshot["pending_calls"] == 5
    assert snapshot["remaining_calls"] == 0
    assert (await usage.snapshot(-100))["calls"] == 3
    assert (await usage.snapshot(-200))["calls"] == 2


@pytest.mark.asyncio
async def test_budget_is_atomic_between_database_connections(db):
    other_db = Database(db.path)
    await other_db.open()
    try:
        stores = [UsageStore(db, global_daily_limit=1), UsageStore(other_db, global_daily_limit=1)]

        async def attempt(store, chat_id):
            try:
                return await store.reserve(chat_id, "reply", "model")
            except BudgetExceeded:
                return None

        results = await asyncio.gather(attempt(stores[0], -100), attempt(stores[1], -200))
        assert sum(item is not None for item in results) == 1
        assert (await stores[0].snapshot(None))["calls"] == 1
    finally:
        await other_db.close()


@pytest.mark.asyncio
async def test_failure_and_unfinished_reservations_survive_restart_and_exhaust_budget(db):
    usage = UsageStore(db, default_chat_daily_limit=2)
    failed = await usage.reserve(-100, "reply", "model")
    await usage.finish(failed, success=False, latency_seconds=0.5, error_type="TimeoutError")
    await usage.reserve(-100, "decision", "model")
    await db.close()
    await db.open()
    usage = UsageStore(db, default_chat_daily_limit=2)
    with pytest.raises(BudgetExceeded):
        await usage.reserve(-100, "reply", "model")
    snapshot = await usage.snapshot(-100)
    assert snapshot["calls"] == 2
    assert snapshot["failures"] == 1
    assert snapshot["pending_calls"] == 1
    assert snapshot["last_error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_local_midnight_resets_daily_budget_without_deleting_old_records(db):
    now = [datetime(2026, 10, 29, 23, 59, 59, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    usage = UsageStore(db, default_chat_daily_limit=1, clock=lambda: now[0])
    await usage.reserve(-100, "reply", "model")
    with pytest.raises(BudgetExceeded):
        await usage.reserve(-100, "reply", "model")
    now[0] += 2
    await usage.reserve(-100, "reply", "model")
    snapshot = await usage.snapshot(-100)
    assert snapshot["day"] == "2026-10-30"
    assert snapshot["calls"] == 1
    async with db.connection.execute("SELECT COUNT(*) FROM ai_call_usage") as cursor:
        assert (await cursor.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_dst_clock_fallback_does_not_reset_same_day_budget(db):
    timezone = ZoneInfo("America/New_York")
    now = [datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=0).timestamp()]
    usage = UsageStore(db, timezone_name="America/New_York", default_chat_daily_limit=1, clock=lambda: now[0])
    await usage.reserve(-100, "reply", "model")
    now[0] = datetime(2026, 11, 1, 1, 30, tzinfo=timezone, fold=1).timestamp()
    with pytest.raises(BudgetExceeded):
        await usage.reserve(-100, "reply", "model")


@pytest.mark.asyncio
async def test_override_and_zero_limit_never_bypass_global_budget(db):
    usage = UsageStore(db, global_daily_limit=2, default_chat_daily_limit=1)
    with pytest.raises(BudgetExceeded):
        await usage.reserve(-100, "reply", "model", chat_limit=0)
    await usage.reserve(-100, "reply", "model", chat_limit=100)
    await usage.reserve(-100, "reply", "model", chat_limit=100)
    with pytest.raises(BudgetExceeded):
        await usage.reserve(-100, "reply", "model", chat_limit=100)
    assert (await usage.snapshot(-100, chat_limit=100))["remaining_calls"] == 0
    disabled = UsageStore(db, global_daily_limit=0)
    with pytest.raises(BudgetExceeded):
        await disabled.reserve(-200, "reply", "model")


@pytest.mark.asyncio
async def test_known_usage_costs_and_missing_usage_are_distinguished(db):
    usage = UsageStore(db)
    known = await usage.reserve(-100, "reply", "model")
    await usage.finish(known, success=True, latency_seconds=2, prompt_tokens=10,
                       completion_tokens=5, estimated_cost_usd=0.001)
    missing = await usage.reserve(-100, "decision", "small-model")
    await usage.finish(missing, success=True, latency_seconds=1)
    snapshot = await usage.snapshot(-100)
    assert snapshot["successes"] == 2
    assert snapshot["missing_usage_calls"] == 1
    assert snapshot["prompt_tokens"] == 10
    assert snapshot["completion_tokens"] == 5
    assert snapshot["average_latency_seconds"] == 1.5
    assert snapshot["estimated_cost_usd"] == 0.001
    assert snapshot["priced_calls"] == 1
    assert not snapshot["cost_estimate_complete"]
    assert (await usage.snapshot(-200))["estimated_cost_usd"] is None


@pytest.mark.asyncio
async def test_first_completion_wins_without_double_counting(db):
    usage = UsageStore(db)
    event_id = await usage.reserve(-100, "reply", "model")
    await usage.finish(event_id, success=True, latency_seconds=1, prompt_tokens=1, completion_tokens=2)
    await usage.finish(event_id, success=False, latency_seconds=2, error_type="CancelledError")
    snapshot = await usage.snapshot(-100)
    assert snapshot["calls"] == snapshot["successes"] == 1
    assert snapshot["failures"] == 0
    assert snapshot["last_error_type"] is None


@pytest.mark.asyncio
async def test_outcomes_are_group_scoped_and_reject_arbitrary_chat_text(db):
    now = [1_800_000_000.0]
    usage = UsageStore(db, clock=lambda: now[0])
    await usage.event(-100, "model_skip")
    await usage.event(-100, "model_skip")
    now[0] += 1
    await usage.event(-100, "reply_sent")
    await usage.event(-200, "budget_exhausted")
    snapshot = await usage.snapshot(-100)
    assert snapshot["events"] == {"reply_sent": 1, "model_skip": 2}
    assert snapshot["last_event"] == "reply_sent"
    assert (await usage.snapshot(None))["events"]["budget_exhausted"] == 1
    with pytest.raises(ValueError):
        await usage.event(-100, "private chat text or a credential")
    with pytest.raises(ValueError):
        event_id = await usage.reserve(-100, "reply", "model")
        await usage.finish(event_id, success=False, latency_seconds=0, error_type="private upstream body")


@pytest.mark.asyncio
async def test_cleanup_preserves_current_day_budget_and_expires_old_statistics(db):
    now = [datetime(2026, 10, 26, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    usage = UsageStore(db, retention_days=1, default_chat_daily_limit=1, clock=lambda: now[0])
    await usage.reserve(-100, "reply", "old-model")
    await usage.event(-100, "reply_sent")
    now[0] += timedelta(days=3).total_seconds()
    await usage.reserve(-100, "reply", "current-model")
    await usage.event(-100, "budget_exhausted")
    await usage.cleanup()
    with pytest.raises(BudgetExceeded):
        await usage.reserve(-100, "reply", "model")
    async with db.connection.execute("SELECT COUNT(*) FROM ai_call_usage") as cursor:
        assert (await cursor.fetchone())[0] == 1
    snapshot = await usage.snapshot(-100)
    assert snapshot["events"] == {"budget_exhausted": 1}
