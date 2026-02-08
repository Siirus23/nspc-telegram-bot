from aiogram import Router, F
from aiogram.types import Message
from datetime import datetime, timedelta

# TEMP: SQLite admin session helpers removed during Supabase migration

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

    if action not in {"claim", "cancel"}:
        return

    key = resolve_channel_post_keys(message)
    if not key:
        return

    channel_chat_id, channel_message_id = key
    if channel_chat_id != CHANNEL_ID:
        return

    with get_db() as conn:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        # Fetch card info
        cur.execute("""
            SELECT card_name, price, remaining_qty
            FROM card_listing
            WHERE channel_chat_id = ?
              AND channel_message_id = ?
        """, (channel_chat_id, channel_message_id))
        card = cur.fetchone()

        if not card:
            await message.reply("❌ This post is not a tracked card.")
            return

        card_name, price, remaining = card

        # =========================
        # CLAIM
        # =========================
        if action == "claim":
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
            cur.execute("""
                SELECT COUNT(*) AS c FROM claims
                WHERE channel_chat_id = ?
                  AND channel_message_id = ?
                  AND user_id = ?
                  AND status = 'active'
            """, (channel_chat_id, channel_message_id, message.from_user.id))
            existing_active = cur.fetchone()["c"]

            if existing_active > 0:
                await message.reply("❌ You already have active claim(s) on this card. To edit claim, type cancel and claim again.")
                return

            # Create claims (multiple rows)
            for _ in range(qty):
                cur.execute("""
                    SELECT COUNT(*) AS c FROM claims
                    WHERE channel_chat_id = ?
                      AND channel_message_id = ?
                      AND status = 'active'
                """, (channel_chat_id, channel_message_id))
                claim_order = cur.fetchone()["c"] + 1

                cur.execute("""
                    SELECT id FROM claims
                    WHERE channel_chat_id = ?
                      AND channel_message_id = ?
                      AND user_id = ?
                      AND status = 'cancelled'
                    ORDER BY id DESC
                    LIMIT 1
                """, (channel_chat_id, channel_message_id, message.from_user.id))
                cancelled_row = cur.fetchone()

                if cancelled_row:
                    claim_id = cancelled_row["id"]
                    cur.execute("""
                        UPDATE claims
                        SET status = 'active',
                            username = ?,
                            claim_order = ?,
                            claimed_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (message.from_user.username, claim_order, claim_id))
                else:
                    cur.execute("""
                        INSERT INTO claims
                        (channel_chat_id, channel_message_id, user_id, username, claim_order)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        channel_chat_id,
                        channel_message_id,
                        message.from_user.id,
                        message.from_user.username,
                        claim_order
                    ))

            cur.execute("""
                UPDATE card_listing
                SET remaining_qty = remaining_qty - ?
                WHERE channel_chat_id = ?
                  AND channel_message_id = ?
            """, (qty, channel_chat_id, channel_message_id))

            new_remaining = remaining - qty

            await message.reply(
                f"✅ Claim Approved @{message.from_user.username or 'user'}\n"
                f"Quantity: {qty}\n"
                f"Remaining: {new_remaining}"
            )

        # =========================
        # CANCEL (cancel ALL owned claims)
        # =========================
        else:
            cur.execute("""
                SELECT id, claimed_at
                FROM claims
                WHERE channel_chat_id = ?
                  AND channel_message_id = ?
                  AND user_id = ?
                  AND status = 'active'
            """, (channel_chat_id, channel_message_id, message.from_user.id))
            claims = cur.fetchall()

            if not claims:
                await message.reply("❌ You don’t have any active claims on this card.")
                return

            total_to_cancel = len(claims)

            earliest_claim = min(c["claimed_at"] for c in claims)
            claimed_time = _parse_sqlite_ts(earliest_claim)

            if message.from_user.id != ADMIN_ID:
                if datetime.utcnow() - claimed_time > timedelta(minutes=CANCEL_WINDOW_MINUTES):
                    await message.reply(
                        f"❌ Cancellation window ({CANCEL_WINDOW_MINUTES} minutes) has passed.\n"
                        "Please contact @ILoveCatFoochie."
                    )
                    return

            cur.execute("""
                UPDATE claims
                SET status = 'cancelled'
                WHERE channel_chat_id = ?
                  AND channel_message_id = ?
                  AND user_id = ?
                  AND status = 'active'
            """, (channel_chat_id, channel_message_id, message.from_user.id))

            cur.execute("""
                UPDATE card_listing
                SET remaining_qty = remaining_qty + ?
                WHERE channel_chat_id = ?
                  AND channel_message_id = ?
            """, (total_to_cancel, channel_chat_id, channel_message_id))

            new_remaining = remaining + total_to_cancel

            await message.reply(
                f"⚠️ All claims cancelled by @{message.from_user.username or 'user'}\n"
                f"Restored: {total_to_cancel}\n"
                f"Available: {new_remaining}"
            )

    # =========================
    # AUTO-EDIT CAPTION
    # =========================
    if new_remaining <= 0:
        caption = f"{card_name}\nPrice: {price}\n❌ SOLD OUT"
    else:
        caption = f"{card_name}\nPrice: {price}\nAvailable: {new_remaining}"

    try:
        await message.bot.edit_message_caption(
            chat_id=channel_chat_id,
            message_id=channel_message_id,
            caption=caption
        )
    except Exception as e:
        print(f"Caption edit failed: {e}")
