import pytest

from app.services.followup import FollowupTracker


@pytest.fixture
def tracker():
    now = [1000.0]
    instance = FollowupTracker(ttl_seconds=120, clock=lambda: now[0])
    instance.record_reply(-100, None, 10, 500)
    return instance, now


@pytest.mark.parametrize("text", [
    "还有呢？", "然后呢", "为什么？", "为什么这么说", "换一个吧", "继续", "请继续！",
    "说详细点", "展开讲讲", "举个例子", "没看懂", "那 Python 呢？", "能不能解释一下？",
    "Continue", "why is that?", "Another one, please!", "give me an example", "explain more",
    "I don't understand", "more details", "please go on",
])
def test_short_clear_followup_signals(tracker, text):
    instance, _ = tracker
    assert instance.classify(-100, None, 10, text) == "followup"


@pytest.mark.parametrize("text", [
    "今天吃什么？", "大家晚上好", "这件事我也遇到过", "继续聊天啊朋友们", "/status", "",
    "🤔", "why does my unrelated project fail", "a" * 200, "那我明天再去买菜",
])
def test_chatter_is_not_implicitly_claimed(tracker, text):
    instance, _ = tracker
    assert instance.classify(-100, None, 10, text) == "none"


def test_group_topic_and_user_isolation(tracker):
    instance, _ = tracker
    for chat_id, thread_id, user_id in [(-200, None, 10), (-100, 20, 10), (-100, None, 11)]:
        assert instance.classify(chat_id, thread_id, user_id, "为什么") == "none"
    instance.record_reply(-100, 20, 11, 501)
    assert instance.classify(-100, 20, 11, "还有呢") == "followup"
    assert instance.classify(-100, None, 10, "还有呢") == "followup"


def test_new_successful_reply_replaces_active_speaker(tracker):
    instance, _ = tracker
    instance.record_reply(-100, None, 11, 501)
    assert instance.classify(-100, None, 10, "为什么") == "none"
    assert instance.classify(-100, None, 11, "为什么") == "followup"


def test_classification_does_not_extend_ttl_but_successful_reply_does(tracker):
    instance, now = tracker
    now[0] += 119
    assert instance.classify(-100, None, 10, "为什么") == "followup"
    now[0] += 1
    assert instance.classify(-100, None, 10, "为什么") == "none"
    instance.record_reply(-100, None, 10, 501)
    now[0] += 119
    instance.record_reply(-100, None, 10, 502)
    now[0] += 119
    assert instance.classify(-100, None, 10, "为什么") == "followup"


def test_messages_addressed_to_other_people_are_not_followups(tracker):
    instance, _ = tracker
    assert instance.classify(-100, None, 10, "为什么", reply_to_user_id=20, bot_user_id=999) == "none"
    assert instance.classify(-100, None, 10, "为什么", reply_to_user_id=10, bot_user_id=999) == "none"
    assert instance.classify(-100, None, 10, "为什么", reply_to_user_id=999, bot_user_id=999) == "followup"
    for text in ["@another_user 继续", "继续 @another_user", "那 @another_user 呢"]:
        assert instance.classify(-100, None, 10, text) == "none"


@pytest.mark.parametrize("text", [
    "先别说话", "不用回复了", "停止回答", "到此为止", "请你先别说话了，谢谢", "stop replying",
    "Stop", "don't reply", "please stop responding", "that's all",
])
def test_stop_signal_ends_only_the_active_session(tracker, text):
    instance, _ = tracker
    assert instance.classify(-100, None, 11, text) == "none"
    assert instance.classify(-100, None, 10, text) == "stop"
    assert instance.classify(-100, None, 10, "为什么") == "none"
    assert instance.classify(-100, None, 10, text) == "none"


def test_expired_or_differently_addressed_stop_does_not_affect_session(tracker):
    instance, now = tracker
    assert instance.classify(-100, None, 10, "stop replying", reply_to_user_id=20, bot_user_id=999) == "none"
    assert instance.classify(-100, None, 10, "为什么") == "followup"
    now[0] += 120
    assert instance.classify(-100, None, 10, "先别说话") == "none"


def test_clear_topic_and_clear_chat(tracker):
    instance, _ = tracker
    instance.record_reply(-100, 20, 10, 501)
    instance.record_reply(-200, None, 10, 502)
    instance.clear(-100)
    assert instance.classify(-100, None, 10, "为什么") == "none"
    assert instance.classify(-100, 20, 10, "为什么") == "followup"
    instance.clear_chat(-100)
    assert instance.classify(-100, 20, 10, "为什么") == "none"
    assert instance.classify(-200, None, 10, "为什么") == "followup"


def test_capacity_evicts_oldest_successful_reply_and_prunes_expiry():
    now = [1000.0]
    instance = FollowupTracker(clock=lambda: now[0], max_sessions=2)
    instance.record_reply(-100, 1, 10, 500)
    now[0] += 1
    instance.record_reply(-100, 2, 10, 501)
    instance.record_reply(-100, 1, 10, 502)
    instance.record_reply(-100, 3, 10, 503)
    assert instance.classify(-100, 2, 10, "为什么") == "none"
    assert instance.classify(-100, 1, 10, "为什么") == "followup"
    assert instance.classify(-100, 3, 10, "为什么") == "followup"
    now[0] += 120
    assert instance.classify(-100, 1, 10, "为什么") == "none"


def test_clock_rollback_expires_session_and_zero_ttl_disables_it(tracker):
    instance, now = tracker
    now[0] -= 1
    assert instance.classify(-100, None, 10, "为什么") == "none"
    disabled = FollowupTracker(ttl_seconds=0)
    disabled.record_reply(-100, None, 10, 500)
    assert disabled.classify(-100, None, 10, "为什么") == "none"


@pytest.mark.parametrize("kwargs", [{"ttl_seconds": -1}, {"ttl_seconds": float("nan")},
                                    {"ttl_seconds": float("inf")}, {"max_sessions": 0}])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        FollowupTracker(**kwargs)
