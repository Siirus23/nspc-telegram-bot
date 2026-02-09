from aiogram import Router, F
from aiogram.types import Message
from datetime import datetime, timedelta

from db import (
    get_card_by_post,
    count_active_claims_for_card,
    user_has_active_claim,
    get_latest_cancelled_claim_id,
    revive_cancelled_claim,
    create_claim,
    update_card_remaining,
)

from config import CHANNEL_ID, ADMIN_ID

router = Router()
CANCEL_WINDOW_MINUTES = 5


def resolve_channel_post_keys(message: Message):
    r = message.reply_to_message
    if not r:
        return None

    if r.forward_from_chat and r.forward_from_message_id:
        return r.forward_from_chat.id, r.forward_from_message_id

    return r.chat.id, r.message_id


def _parse_sqlite_ts(ts: str) -> datetime:
    # SQLite CURRENT_TIMESTAMP usually: "YYYY-MM-DD HH:MM:SS"
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except Exception:
        # fallback attempt
        try:
            return datetime.fromisoformat(ts)
        except Exception:
            return datetime.utcnow()


@router.message(F.reply_to_message, F.edit_date.is_(None))
async def handle_claim_and_cancel(message: Message):
    raw = message.text.strip().lower() if message.text else ""
    parts = raw.split()
    action = parts[0] if parts else ""

    if action != "claim":
        return  # 🚫 cancel not migrated yet

    key = resolve_channel_post_keys(message)
    if not key:
        return

    channel_chat_id, channel_message_id = key
    if channel_chat_id != CHANNEL_ID:
        return

    # 🔹 Fetch card info (Supabase)
    card = await get_card_by_post(channel_chat_id, channel_message_id)
    if not card:
        await message.reply("❌ This post is not a tracked card.")
        return

    card_name = card["card_name"]
    price = card["price"]
    remaining = card["remaining_qty"]

    # =========================
    # CLAIM
    # =========================
    qty = 1

    if len(parts) > 1 and parts[1] == "all":
        qty = remaining
    elif len(parts) > 1:
        if parts[1].isdigit():
            qty = int(parts[1])
        else:
            await message.reply("❌ Invalid format. Use: 'claim', 'claim 2', or 'claim all'")
            return

    if qty <= 0:
        await message.reply("❌ Nothing available to claim.")
        return

    if remaining <= 0:
        await message.reply("❌ Card is Fully Claimed")
        return

    if qty > remaining:
        await message.reply(f"❌ Only {remaining} remaining. You cannot claim {qty}.")
        return

    # Prevent multiple separate claims
    if await user_has_active_claim(channel_chat_id, channel_message_id, message.from_user.id):
        await message.reply(
            "❌ You already have active claim(s) on this card. "
            "To edit claim, type cancel and claim again."
        )
        return

    # Create claims
    for _ in range(qty):
        claim_order = await count_active_claims_for_card(
            channel_chat_id,
            channel_message_id,
        ) + 1

        cancelled_id = await get_latest_cancelled_claim_id(
            channel_chat_id,
            channel_message_id,
            message.from_user.id,
        )

        if cancelled_id:
            await revive_cancelled_claim(
                claim_id=cancelled_id,
                username=message.from_user.username,
                claim_order=claim_order,
            )
        else:
            await create_claim(
                channel_chat_id=channel_chat_id,
                channel_message_id=channel_message_id,
                user_id=message.from_user.id,
                username=message.from_user.username,
                claim_order=claim_order,
            )

    # Reduce availability
    await update_card_remaining(
        channel_chat_id,
        channel_message_id,
        delta=-qty,
    )

    new_remaining = remaining - qty

    await message.reply(
        f"✅ Claim Approved @{message.from_user.username or 'user'}\n"
        f"Quantity: {qty}\n"
        f"Remaining: {new_remaining}"
    )
