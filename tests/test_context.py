import asyncio

import pytest
import pytest_asyncio

from app.storage.database import Database
from app.storage.messages import MessageStore, StoredMessage


@pytest_asyncio.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "nested" / "context.db"))
    await database.open()
    yield database
    await database.close()


def message(number, *, chat=-100, topic=None, created_at=None, role="user"):
    return StoredMessage(
        chat_id=chat, message_id=number, user_id=42,
        username="example", role=role, content=f"message {number}",
        created_at=number if created_at is None else created_at,
        thread_id=topic,
    )


@pytest.mark.asyncio
async def test_context_is_bounded_chronological_and_group_isolated(db):
    store = MessageStore(db, max_messages=3, clock=lambda: 1000)
    # Telegram updates can arrive out of order.
    for number in (4, 1, 3, 2, 5):
        await store.add(message(number))
    await store.add(message(1, chat=-200))
    assert [item.message_id for item in await store.recent(-100)] == [3, 4, 5]
    assert [item.message_id for item in await store.recent(-200)] == [1]
    assert await store.count(-100) == 3
    assert len(await store.recent(-100, limit=100)) == 3
    assert await store.recent(-100, limit=0) == []


@pytest.mark.asyncio
async def test_topics_are_isolated_but_share_group_retention_budget(db):
    store = MessageStore(db, max_messages=3, clock=lambda: 1000)
    await store.add(message(1))
    await store.add(message(2, topic=10))
    await store.add(message(3, topic=20))
    await store.add(message(4, topic=10))
    assert await store.recent(-100) == []
    assert [item.message_id for item in await store.recent(-100, thread_id=10)] == [2, 4]
    assert [item.message_id for item in await store.recent(-100, thread_id=20)] == [3]
    assert await store.count(-100) == 3


@pytest.mark.asyncio
async def test_dedup_survives_context_pruning_and_restart(db):
    store = MessageStore(db, max_messages=1, clock=lambda: 1000)
    assert await store.add(message(1))
    assert await store.add(message(2))
    assert not await store.add(message(1))
    await db.close()
    await db.open()
    restarted = MessageStore(db, max_messages=1, clock=lambda: 1001)
    assert not await restarted.add(message(1))
    assert not await restarted.add(message(2))
    assert [item.message_id for item in await restarted.recent(-100)] == [2]


@pytest.mark.asyncio
async def test_simultaneous_duplicate_inserts_accept_only_one(db):
    store = MessageStore(db, clock=lambda: 1000)
    results = await asyncio.gather(*(store.add(message(1)) for _ in range(20)))
    assert sum(results) == 1
    assert await store.count(-100) == 1
    assert await store.add(message(1, chat=-200))


@pytest.mark.asyncio
async def test_retention_expires_inactive_chats_and_seen_ledger(db):
    now = [1000.0]
    store = MessageStore(db, retention_days=1, clock=lambda: now[0])
    await store.add(message(1, created_at=now[0]))
    await store.set_paused(-100, True)
    now[0] += 86401
    # Expired data is never returned even before the maintenance task runs.
    assert await store.recent(-100) == []
    assert await store.count(-100) == 0
    await store.cleanup()
    async with db.lock:
        async with db.connection.execute("SELECT COUNT(*) FROM seen_messages") as cursor:
            assert (await cursor.fetchone())[0] == 0
    assert await store.is_paused(-100)
    assert await store.add(message(1, created_at=now[0]))


@pytest.mark.asyncio
async def test_pause_and_context_survive_database_restart(db):
    store = MessageStore(db, clock=lambda: 1000)
    await store.add(message(1))
    assert not await store.is_paused(-100)
    await store.set_paused(-100, True)
    await db.close()
    await db.open()
    assert await store.is_paused(-100)
    assert not await store.is_paused(-200)
    assert await store.count(-100) == 1
    await store.set_paused(-100, False)
    assert not await store.is_paused(-100)


@pytest.mark.asyncio
async def test_cleanup_applies_reduced_context_limit_to_all_chats(db):
    store = MessageStore(db, max_messages=10, clock=lambda: 1000)
    for chat in (-100, -200):
        for number in range(1, 5):
            await store.add(message(number, chat=chat))
    smaller_store = MessageStore(db, max_messages=2, clock=lambda: 1000)
    await smaller_store.cleanup()
    for chat in (-100, -200):
        assert [item.message_id for item in await smaller_store.recent(chat)] == [3, 4]
        assert not await smaller_store.add(message(1, chat=chat))


@pytest.mark.asyncio
async def test_message_metadata_and_assistant_role_roundtrip(db):
    store = MessageStore(db, clock=lambda: 1000)
    original = StoredMessage(-100, 1, None, None, "assistant", "你好！", 999.5, 10)
    assert await store.add(original)
    assert await store.recent(-100, thread_id=10) == [original]
    with pytest.raises(ValueError):
        await store.add(message(2, role="system"))
    assert await store.count(-100) == 1
