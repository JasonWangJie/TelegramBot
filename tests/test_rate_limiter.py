import asyncio

import pytest
import pytest_asyncio

from app.services.rate_limiter import RateLimiter
from app.storage.database import Database
from app.storage.messages import MessageStore, StoredMessage


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "limits.db"))
    await database.open()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_attempt_check_is_atomic_and_final_check_ignores_attempt_cooldown(db):
    now = [1000.0]
    limiter = RateLimiter(db, clock=lambda: now[0])
    results = await asyncio.gather(*(limiter.try_start_attempt(-100) for _ in range(20)))
    assert sum(result.allowed for result in results) == 1
    assert all(result.reason == "attempt_cooldown" for result in results if not result.allowed)
    assert (await limiter.check(-100)).allowed
    assert (await limiter.try_start_attempt(-200)).allowed
    now[0] += 8
    assert (await limiter.try_start_attempt(-100)).allowed


@pytest.mark.asyncio
async def test_reply_interval_boundary_and_separate_groups(db):
    now = [1000.0]
    limiter = RateLimiter(db, clock=lambda: now[0])
    await limiter.record_reply(-100)
    now[0] += 19
    result = await limiter.check(-100)
    assert not result.allowed
    assert result.reason == "reply_interval"
    assert result.retry_after == 1
    assert result.remaining == 11
    assert (await limiter.check(-200)).allowed
    now[0] += 1
    assert (await limiter.check(-100)).allowed


@pytest.mark.asyncio
async def test_hourly_quota_uses_rolling_window(db):
    now = [1000.0]
    limiter = RateLimiter(db, min_interval_seconds=0, max_per_hour=2, clock=lambda: now[0])
    await limiter.record_reply(-100)
    now[0] += 10
    await limiter.record_reply(-100)
    result = await limiter.check(-100)
    assert not result.allowed
    assert result.reason == "hourly_quota"
    assert result.remaining == 0
    assert result.retry_after == 3590
    now[0] = 4600
    result = await limiter.check(-100)
    assert result.allowed
    assert result.remaining == 1


@pytest.mark.asyncio
async def test_limits_and_attempt_cooldown_survive_restart(db):
    now = [1000.0]
    limiter = RateLimiter(db, clock=lambda: now[0])
    assert (await limiter.try_start_attempt(-100)).allowed
    await limiter.record_reply(-200)
    await db.close()
    await db.open()
    limiter = RateLimiter(db, clock=lambda: now[0])
    assert not (await limiter.try_start_attempt(-100)).allowed
    assert not (await limiter.check(-200)).allowed
    assert (await limiter.check(-200)).remaining == 11


@pytest.mark.asyncio
async def test_pause_update_does_not_destroy_attempt_or_backoff(db):
    now = [1000.0]
    limiter = RateLimiter(db, clock=lambda: now[0])
    store = MessageStore(db, clock=lambda: now[0])
    await limiter.try_start_attempt(-100)
    await limiter.defer(-100, 60)
    await store.set_paused(-100, True)
    assert await store.is_paused(-100)
    assert (await limiter.check(-100)).retry_after == 60
    await limiter.defer(-100, 5)
    assert (await limiter.check(-100)).retry_after == 60
    now[0] += 60
    assert (await limiter.try_start_attempt(-100)).allowed
    assert await store.is_paused(-100)


@pytest.mark.asyncio
async def test_context_pruning_never_frees_hourly_quota(db):
    now = [1000.0]
    limiter = RateLimiter(db, min_interval_seconds=0, max_per_hour=1, clock=lambda: now[0])
    store = MessageStore(db, max_messages=1, retention_days=0.001, clock=lambda: now[0])
    await limiter.record_reply(-100)
    await store.add(StoredMessage(-100, 1, 1, "bot", "assistant", "reply", now[0]))
    now[0] += 100
    await store.add(StoredMessage(-100, 2, 2, "user", "user", "new", now[0]))
    await store.cleanup()
    assert await store.count(-100) == 1
    result = await limiter.check(-100)
    assert not result.allowed
    assert result.reason == "hourly_quota"


@pytest.mark.asyncio
async def test_lowered_quota_waits_for_enough_replies_to_expire(db):
    now = [1000.0]
    limiter = RateLimiter(db, min_interval_seconds=0, max_per_hour=3, clock=lambda: now[0])
    for timestamp in (1000, 1100, 1200):
        now[0] = timestamp
        await limiter.record_reply(-100)
    stricter = RateLimiter(db, min_interval_seconds=0, max_per_hour=1, clock=lambda: now[0])
    assert (await stricter.check(-100)).retry_after == 3600
    now[0] = 4700
    assert not (await stricter.check(-100)).allowed
    now[0] = 4800
    assert (await stricter.check(-100)).allowed


@pytest.mark.asyncio
async def test_intervals_longer_than_one_hour_survive_history_cleanup(db):
    now = [1000.0]
    limiter = RateLimiter(db, min_interval_seconds=7200, clock=lambda: now[0])
    await limiter.record_reply(-100)
    now[0] += 4000
    await limiter.record_reply(-200)
    result = await limiter.check(-100)
    assert not result.allowed
    assert result.reason == "reply_interval"
    assert result.retry_after == 3200
    assert result.remaining == 12


@pytest.mark.asyncio
async def test_backoff_survives_restart_and_never_shortens(db):
    now = [1000.0]
    limiter = RateLimiter(db, clock=lambda: now[0])
    await limiter.defer(-100, 100)
    await limiter.defer(-100, 10)
    await db.close()
    await db.open()
    status = await limiter.check(-100)
    assert status.reason == "telegram_backoff"
    assert status.retry_after == 100
    now[0] += 100
    assert (await limiter.check(-100)).allowed


@pytest.mark.asyncio
async def test_successful_delivery_finalizes_same_reservation_and_survives_restart(db):
    now = [1000.0]
    limiter = RateLimiter(db, min_interval_seconds=20, max_per_hour=2, clock=lambda: now[0])
    event_id = await limiter.record_reply(-100)
    # Telegram takes longer than the configured reply interval to acknowledge.
    now[0] += 30
    assert (await limiter.check(-100)).allowed
    await limiter.finalize_reply(-100, event_id)
    status = await limiter.check(-100)
    assert not status.allowed
    assert status.reason == "reply_interval"
    assert status.retry_after == 20
    assert status.remaining == 1
    await db.close()
    await db.open()
    restarted = RateLimiter(db, min_interval_seconds=20, max_per_hour=2, clock=lambda: now[0])
    assert (await restarted.check(-100)).retry_after == 20
    assert (await restarted.check(-100)).remaining == 1
    now[0] += 20
    assert (await restarted.check(-100)).allowed
    # The rolling window is based on the acknowledged timestamp too.
    now[0] = 4610
    assert (await restarted.check(-100)).remaining == 1
    now[0] = 4630
    assert (await restarted.check(-100)).remaining == 2


@pytest.mark.asyncio
async def test_finalize_cannot_update_another_groups_reservation(db):
    now = [1000.0]
    limiter = RateLimiter(db, clock=lambda: now[0])
    event_id = await limiter.record_reply(-100)
    now[0] += 10
    with pytest.raises(ValueError):
        await limiter.finalize_reply(-200, event_id)
    assert (await limiter.check(-100)).retry_after == 10
    assert (await limiter.check(-200)).remaining == 12
