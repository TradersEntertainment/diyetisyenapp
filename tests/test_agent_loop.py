"""Exercise the conversational agent loop with a mocked Anthropic client.

Verifies the tool-use loop wiring: user message -> assistant tool_use ->
tool executed against the DB -> tool_result fed back -> final assistant text,
and that the exchange is persisted to ConversationMessage.
"""


from app.models import ConversationMessage, Profile, User, WeightLog
from app.services.calculations import GOAL_LOSE, compute_targets
from app.services.targets import save_targets


class _Block:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type = type
        self.text = text
        self.name = name
        self.input = input or {}
        self.id = id


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class _FakeMessages:
    """Returns a scripted sequence of responses on successive .create() calls."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses):
        self.messages = _FakeMessages(responses)


async def _make_active_user(session):
    user = User(telegram_id=111, name="Ayşe", onboarding_state="active")
    session.add(user)
    await session.flush()
    session.add(
        Profile(
            user_id=user.id, age=30, gender="kadin", height_cm=165,
            start_weight_kg=70, goal_weight_kg=63, body_fat_pct=28,
            activity_level="hafif_aktif", goals=[GOAL_LOSE], exercise_frequency_per_week=3,
        )
    )
    session.add(WeightLog(user_id=user.id, weight_kg=70))
    t = compute_targets(
        weight_kg=70, height_cm=165, age=30, gender="kadin",
        activity_level="hafif_aktif", primary_goal=GOAL_LOSE, body_fat_pct=28,
    )
    await save_targets(session, user.id, t, diet_strategy="dengeli", reason="init")
    await session.flush()
    return user


async def test_chat_runs_tool_then_replies(session, monkeypatch):
    user = await _make_active_user(session)

    # Turn 1: model asks to log a weight. Turn 2: model gives final text.
    responses = [
        _Response(
            [
                _Block("text", text="Tabii, kaydediyorum."),
                _Block("tool_use", name="log_weight", input={"weight_kg": 69.4}, id="t1"),
            ],
            stop_reason="tool_use",
        ),
        _Response([_Block("text", text="69.4 kg kaydedildi, harika gidiyorsun! 💪")], stop_reason="end_turn"),
    ]
    fake = _FakeClient(responses)
    monkeypatch.setattr("app.ai.dietitian.get_client", lambda: fake)
    monkeypatch.setattr("app.ai.dietitian.get_model", lambda: "test-model")

    from app.ai.dietitian import chat

    reply = await chat(session, user, "bugün 69.4 kiloyum")

    assert "69.4" in reply
    # weight actually persisted by the tool
    res = await session.execute(WeightLog.__table__.select().where(WeightLog.user_id == user.id))
    assert len(res.fetchall()) == 2  # starting + new

    # conversation persisted (user + assistant)
    res = await session.execute(
        ConversationMessage.__table__.select().where(ConversationMessage.user_id == user.id)
    )
    rows = res.fetchall()
    roles = {r.role for r in rows}
    assert roles == {"user", "assistant"}

    # second API call carried the tool_result back
    second_call_messages = fake.messages.calls[1]["messages"]
    assert any(
        isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
        for m in second_call_messages
    )


async def test_chat_handles_refusal(session, monkeypatch):
    user = await _make_active_user(session)
    fake = _FakeClient([_Response([], stop_reason="refusal")])
    monkeypatch.setattr("app.ai.dietitian.get_client", lambda: fake)
    monkeypatch.setattr("app.ai.dietitian.get_model", lambda: "test-model")

    from app.ai.dietitian import FALLBACK_TEXT, chat

    reply = await chat(session, user, "merhaba")
    assert reply == FALLBACK_TEXT


async def test_system_blocks_have_cache_control():
    from app.ai.dietitian import _system_blocks

    blocks = _system_blocks("örnek bağlam")
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert "örnek bağlam" in blocks[1]["text"]
