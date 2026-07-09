"""The conversational dietitian agent + AI narrative helpers."""
import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import get_client, get_model
from app.ai.context import build_user_context
from app.ai.prompts import DIETITIAN_PERSONA, STRATEGY_DECISION_PROMPT
from app.ai.tools import TOOLS, execute_tool
from app.models import ConversationMessage, User
from app.services.adaptive import AdjustmentDecision
from app.services.analysis import WeeklyStats
from app.services.calculations import Targets
from app.services.reports import format_weekly_stats_tr

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
HISTORY_MESSAGES = 30
SUMMARIZE_AFTER = 80  # unsummarized messages
SUMMARIZE_BATCH = 50

FALLBACK_TEXT = "Şu an yanıt üretemedim, birazdan tekrar dener misin? 🙏"

SILENT_SENTINEL = "[SESSIZ]"


def _system_blocks(user_context: str, *, group_mode: bool = False, speaker: str = "") -> list[dict]:
    """Frozen persona (cached) + volatile per-user context (after the cache breakpoint)."""
    blocks = [
        {"type": "text", "text": DIETITIAN_PERSONA, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "# KULLANICI BAĞLAMI\n\n" + user_context},
    ]
    if group_mode:
        blocks.append(
            {
                "type": "text",
                "text": (
                    "# ORTAM\nBu konuşma ikisinin de içinde olduğu ortak Telegram grubunda geçiyor. "
                    f"Şu an yazan kişi: {speaker}. Yanıtın gruba gidecek; {speaker} ismiyle hitap et. "
                    "Mesaj sana yönelik değilse ve katkın gerekmiyorsa sadece [SESSIZ] yaz."
                ),
            }
        )
    return blocks


async def _load_history(session: AsyncSession, user_id: int) -> list[dict]:
    res = await session.execute(
        select(ConversationMessage)
        .where(ConversationMessage.user_id == user_id)
        .order_by(ConversationMessage.ts.desc(), ConversationMessage.id.desc())
        .limit(HISTORY_MESSAGES)
    )
    rows = list(res.scalars())[::-1]
    return [{"role": r.role, "content": r.content} for r in rows if r.content.strip()]


async def chat(
    session: AsyncSession,
    user: User,
    text: str,
    *,
    group_mode: bool = False,
    image: tuple[str, str] | None = None,
) -> str:
    """One conversational turn: log data via tools, reply in Turkish.

    In group_mode the model may answer with SILENT_SENTINEL to stay out of a
    conversation between the two humans; the caller then sends nothing.
    image is an optional (base64_data, media_type) pair, e.g. a photo of a meal.
    """
    client = get_client()
    context = await build_user_context(session, user)
    history = await _load_history(session, user.id)

    session.add(ConversationMessage(user_id=user.id, role="user", content=text))

    content: str | list
    if image:
        content = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": image[1], "data": image[0]},
            },
            {"type": "text", "text": text},
        ]
    else:
        content = text
    messages: list = history + [{"role": "user", "content": content}]
    reply_parts: list[str] = []

    for _ in range(MAX_TOOL_ROUNDS):
        response = await client.messages.create(
            model=get_model(),
            max_tokens=8000,
            system=_system_blocks(context, group_mode=group_mode, speaker=user.name or "kullanıcı"),
            tools=TOOLS,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            log.warning("model refused for user %s", user.id)
            return FALLBACK_TEXT

        for block in response.content:
            if block.type == "text" and block.text.strip():
                reply_parts.append(block.text.strip())

        if response.stop_reason != "tool_use":
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = await execute_tool(session, user, block.name, dict(block.input))
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result}
                )
        messages.append({"role": "user", "content": tool_results})
    else:
        log.warning("tool loop hit MAX_TOOL_ROUNDS for user %s", user.id)

    reply = "\n\n".join(reply_parts).strip() or FALLBACK_TEXT
    session.add(ConversationMessage(user_id=user.id, role="assistant", content=reply))
    await maybe_summarize_history(session, user)
    return reply


async def generate_message(
    session: AsyncSession,
    user: User,
    instruction: str,
    *,
    include_context: bool = True,
    group_mode: bool = False,
) -> str:
    """One-shot Turkish message (greetings, reports, motivational nudges). No tools."""
    client = get_client()
    system: list[dict] = [
        {"type": "text", "text": DIETITIAN_PERSONA, "cache_control": {"type": "ephemeral"}}
    ]
    if include_context:
        context = await build_user_context(session, user)
        system.append({"type": "text", "text": "# KULLANICI BAĞLAMI\n\n" + context})
    if group_mode:
        system.append(
            {
                "type": "text",
                "text": (
                    "# ORTAM\nBu mesaj ikisinin de içinde olduğu ortak Telegram grubuna gidecek. "
                    f"Mesaj {user.name or 'kullanıcı'} için; ona ismiyle hitap et ki kime yazdığın belli olsun."
                ),
            }
        )
    try:
        response = await client.messages.create(
            model=get_model(),
            max_tokens=4000,
            system=system,
            messages=[{"role": "user", "content": instruction}],
        )
    except Exception:
        log.exception("generate_message failed for user %s", user.id)
        return ""
    if response.stop_reason == "refusal":
        return ""
    return next((b.text.strip() for b in response.content if b.type == "text"), "")


STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "diet_strategy": {
            "type": "string",
            "description": "Kısa strateji adı, ör. 'dengeli', 'düşük karbonhidrat', 'Akdeniz', 'antrenman odaklı karbonhidrat'",
        },
        "strategy_changed": {"type": "boolean"},
        "user_message": {
            "type": "string",
            "description": "Kullanıcıya gidecek sıcak, kısa haftalık değerlendirme + strateji açıklaması (Türkçe)",
        },
    },
    "required": ["diet_strategy", "strategy_changed", "user_message"],
    "additionalProperties": False,
}


async def decide_strategy(
    session: AsyncSession,
    user: User,
    stats: WeeklyStats,
    decision: AdjustmentDecision,
    new_targets: Targets,
    current_strategy: str,
) -> tuple[str, str]:
    """Weekly AI strategy choice. Numbers are fixed constraints; Claude picks the approach.

    Returns (diet_strategy, user_message). Falls back to the current strategy on failure.
    """
    client = get_client()
    facts = format_weekly_stats_tr(stats)
    payload = {
        "mevcut_strateji": current_strategy,
        "yeni_hedefler": {
            "kcal": new_targets.kcal,
            "protein_g": new_targets.protein_g,
            "protein_tabani_g": new_targets.protein_floor_g,
            "karbonhidrat_g": new_targets.carb_g,
            "yag_g": new_targets.fat_g,
            "lif_g": new_targets.fiber_g,
        },
        "kural_motoru_ayarlamalari": decision.reasons,
        "karbonhidrat_vurgusu_istegi": decision.carb_bias,
        "hacim_vurgusu_istegi": decision.volume_bias,
        "aktivite_onerisi": decision.activity_nudge,
    }
    context = await build_user_context(session, user)
    try:
        response = await client.messages.create(
            model=get_model(),
            max_tokens=4000,
            system=[
                {"type": "text", "text": DIETITIAN_PERSONA, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": "# KULLANICI BAĞLAMI\n\n" + context},
            ],
            output_config={"format": {"type": "json_schema", "schema": STRATEGY_SCHEMA}},
            messages=[
                {
                    "role": "user",
                    "content": STRATEGY_DECISION_PROMPT
                    + "\n\n## Haftalık veriler\n"
                    + facts
                    + "\n\n## Sayısal durum\n"
                    + json.dumps(payload, ensure_ascii=False, indent=2),
                }
            ],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("refusal")
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        return data["diet_strategy"], data["user_message"]
    except Exception:
        log.exception("decide_strategy failed for user %s", user.id)
        return current_strategy, ""


async def maybe_summarize_history(session: AsyncSession, user: User) -> None:
    """Compress old conversation spans into a long-term memory note."""
    res = await session.execute(
        select(ConversationMessage)
        .where(ConversationMessage.user_id == user.id, ConversationMessage.summarized.is_(False))
        .order_by(ConversationMessage.ts, ConversationMessage.id)
    )
    rows = list(res.scalars())
    if len(rows) < SUMMARIZE_AFTER:
        return
    batch = rows[:SUMMARIZE_BATCH]
    transcript = "\n".join(f"{'K' if r.role == 'user' else 'D'}: {r.content[:400]}" for r in batch)
    client = get_client()
    try:
        response = await client.messages.create(
            model=get_model(),
            max_tokens=2000,
            system=[{"type": "text", "text": DIETITIAN_PERSONA, "cache_control": {"type": "ephemeral"}}],
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Aşağıdaki eski sohbet dökümünü, ileride hatırlamaya değer bilgiler "
                        "(tercihler, alışkanlıklar, olaylar, kararlar) odaklı 5-10 maddelik "
                        "kısa bir Türkçe özete dönüştür:\n\n" + transcript
                    ),
                }
            ],
        )
        summary = next((b.text.strip() for b in response.content if b.type == "text"), "")
    except Exception:
        log.exception("history summarization failed for user %s", user.id)
        return
    if summary:
        from app.models import MemoryNote

        session.add(MemoryNote(user_id=user.id, category="ozet", text=summary))
        for r in batch:
            r.summarized = True
