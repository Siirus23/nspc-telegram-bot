import re
from datetime import datetime
from typing import Optional, Tuple

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_ID, CHANNEL_ID, CHANNEL_USERNAME
from db import get_db
from invoice_pdf import build_invoice_pdf

router = Router()

# ====== CONFIG ======
PAYNOW_NUMBER = "93385994"
PAYNOW_NAME = "Naufal"

TRACKED_FEE_SGD = 3.50
SELF_PICKUP_TEXT = "806 Woodlands St 81, in front of Rainbow Mart"

TRACKING_REGEX = re.compile(r"^RC\d{9}SG$", re.IGNORECASE)

ADMIN_SHIP_SESSION = {}

# ====== HELPERS ======

def now_invoice_no() -> str:
    return f"INV-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

def parse_price_to_float(price_str: str) -> float:
    s = (price_str or "").strip().upper()
    s = s.replace("SGD", "").replace("$", "").strip()
    try:
        return float(s)
    except:
        return 0.0

def make_post_link(channel_chat_id: int, channel_username: str, post_mid: int) -> str:
    username = (channel_username or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{post_mid}"

    s = str(abs(int(channel_chat_id)))
    internal = s[3:] if s.startswith("100") and len(s) > 3 else s
    return f"https://t.me/c/{internal}/{post_mid}"

def kb_delivery():
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📮 Tracked Mail (+${TRACKED_FEE_SGD:.2f})", callback_data="delivery:tracked")
    kb.button(text="🏠 Self Collection (Free)", callback_data="delivery:self")
    kb.button(text="🙋 Contact Human", callback_data="delivery:human")
    kb.adjust(1)
    return kb.as_markup()

def kb_yes_no_browse():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yes", callback_data="browse:yes")
    kb.button(text="➡️ No, continue", callback_data="browse:no")
    kb.adjust(2)
    return kb.as_markup()

def kb_continue():
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Continue", callback_data="checkout:continue")
    kb.adjust(1)
    return kb.as_markup()

def kb_confirm_address():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yes, Confirm", callback_data="addr:confirm")
    kb.button(text="❌ No, re-enter", callback_data="addr:reenter")
    kb.adjust(2)
    return kb.as_markup()

def kb_open_post_button(url: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="📌 Open Post in Channel", url=url)
    kb.adjust(1)
    return kb.as_markup()

def upsert_checkout(user_id: int, **fields):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM user_checkout WHERE user_id = ?", (user_id,))
        exists = cur.fetchone() is not None

        if not exists:
            cur.execute("INSERT INTO user_checkout (user_id) VALUES (?)", (user_id,))

        sets = []
        vals = []
        for k, v in fields.items():
            sets.append(f"{k} = ?")
            vals.append(v)

        sets.append("updated_at = CURRENT_TIMESTAMP")
        vals.append(user_id)

        cur.execute(f"UPDATE user_checkout SET {', '.join(sets)} WHERE user_id = ?", vals)

def get_checkout(user_id: int) -> Optional[dict]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM user_checkout WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None

# ==========================================================
# BUYER FLOW
# ==========================================================

def get_user_claims_summary(user_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                cl.card_name AS card_name,
                cl.price AS price_str,
                cl.channel_message_id AS post_mid,
                COUNT(*) AS qty,
                MIN(c.claim_order) AS first_order
            FROM claims c
            JOIN card_listing cl
              ON cl.channel_chat_id = c.channel_chat_id
             AND cl.channel_message_id = c.channel_message_id
            WHERE c.channel_chat_id = ?
              AND c.user_id = ?
              AND c.status = 'active'
            GROUP BY cl.card_name, cl.price, cl.channel_message_id
            ORDER BY first_order ASC
        """, (CHANNEL_ID, user_id))

        rows = cur.fetchall()

    items = []
    for r in rows:
        items.append({
            "card_name": r["card_name"],
            "price": parse_price_to_float(r["price_str"]),
            "post_mid": int(r["post_mid"]),
            "qty": int(r["qty"]),
        })

    return items


def format_claim_summary(items):
    total = 0.0
    lines = ["🎴 <b>Your Claimed Cards</b>\n"]

    for i, it in enumerate(items, start=1):
        card = it["card_name"]
        qty = it["qty"]
        price = float(it["price"])

        total += price * qty

        if qty == 1:
            lines.append(f"{i}. {card}\n   💰 ${price:.2f} SGD")
        else:
            lines.append(f"{i}. {card} (x{qty})\n   💰 ${price:.2f} SGD each")

    lines.append(f"\n<b>Total: ${total:.2f} SGD</b>")
    return "\n".join(lines), total


@router.message(F.chat.type == "private", Command("start"))
async def dm_start(message: Message):

    await message.answer(
        "👋 Thanks for starting the bot!\n\n"
        "🎉 Your invoice is ready! Please follow the instructions step by step.\n"
        "📄 You will receive your invoice very soon.\n\n"
        "Thank you for your patience! 🙂"
    )

    items = get_user_claims_summary(message.from_user.id)

    if not items:
        await message.answer("⚠️ You have no active claims right now.")
        upsert_checkout(message.from_user.id, stage="idle")
        return

    summary_text, cards_total = format_claim_summary(items)

    upsert_checkout(
        message.from_user.id,
        stage="choose_delivery",
        cards_total=cards_total,
        delivery_fee=0,
        total=cards_total,
    )

    await message.answer(
        summary_text +
        "\n\n📦 <b>Choose your delivery method:</b>\n"
        f"• Tracked Mail: +${TRACKED_FEE_SGD:.2f} delivery charge\n"
        "• Self Collection: No extra charge\n"
        f"📍 Pickup: {SELF_PICKUP_TEXT}\n\n"
        "Please select your preferred option below:",
        parse_mode="HTML",
        reply_markup=kb_delivery()
    )


@router.callback_query(F.data.startswith("delivery:"))
async def delivery_pick(cb: CallbackQuery):

    user_id = cb.from_user.id
    choice = cb.data.split(":", 1)[1]

    if choice == "human":
        await cb.message.answer("🙋 Please DM the admin for help.")
        await cb.answer()
        return

    if choice == "tracked":
        delivery_fee = TRACKED_FEE_SGD
        method = "tracked"
    else:
        delivery_fee = 0
        method = "self"

    ck = get_checkout(user_id) or {}
    cards_total = float(ck.get("cards_total") or 0)

    total = cards_total + delivery_fee

    upsert_checkout(
        user_id,
        delivery_method=method,
        delivery_fee=delivery_fee,
        total=total,
        stage="awaiting_browse"
    )

    await cb.message.answer(
        "Would you like to see cards that are not yet claimed or still have copies left?",
        reply_markup=kb_yes_no_browse()
    )

    await cb.answer()


def list_available_cards(limit=30):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT card_name, price, remaining_qty, channel_message_id
            FROM card_listing
            WHERE channel_chat_id = ?
              AND remaining_qty > 0
            ORDER BY channel_message_id ASC
            LIMIT ?
        """, (CHANNEL_ID, limit))
        return cur.fetchall()


@router.callback_query(F.data.startswith("browse:"))
async def browse_remaining(cb: CallbackQuery):

    user_id = cb.from_user.id
    choice = cb.data.split(":", 1)[1]

    if choice == "yes":
        rows = list_available_cards()

        if not rows:
            await cb.message.answer("✅ No available cards left right now.")
        else:
            for r in rows:
                card_name = r["card_name"]
                price = parse_price_to_float(r["price"])
                remaining = r["remaining_qty"]
                post_mid = r["channel_message_id"]

                url = make_post_link(CHANNEL_ID, CHANNEL_USERNAME, post_mid)

                caption = (
                    f"🃏 <b>{card_name}</b>\n"
                    f"Price: ${price:.2f} SGD\n"
                    f"Available: {remaining}"
                )

                try:
                    await cb.message.bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=CHANNEL_ID,
                        message_id=post_mid,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=kb_open_post_button(url)
                    )
                except:
                    await cb.message.answer(
                        caption,
                        parse_mode="HTML",
                        reply_markup=kb_open_post_button(url)
                    )

    upsert_checkout(user_id, stage="awaiting_continue")

    await cb.message.answer(
        "When you are done claiming and want to proceed with your invoice, tap Continue.",
        reply_markup=kb_continue()
    )

    await cb.answer()


@router.callback_query(F.data == "checkout:continue")
async def checkout_continue(cb: CallbackQuery):

    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    method = ck.get("delivery_method")

    if method not in ("tracked", "self"):
        await cb.message.answer("❌ Please restart with /start.")
        await cb.answer()
        return

    items = get_user_claims_summary(user_id)

    if not items:
        await cb.message.answer("⚠️ You have no active claims.")
        await cb.answer()
        return

    summary_text, cards_total = format_claim_summary(items)

    delivery_fee = float(ck.get("delivery_fee") or 0)
    total = cards_total + delivery_fee

    invoice_no = now_invoice_no()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO orders
            (invoice_no, user_id, username, delivery_method, cards_total, delivery_fee, total)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            invoice_no,
            user_id,
            cb.from_user.username or "",
            method,
            cards_total,
            delivery_fee,
            total
        ))

        order_id = cur.lastrowid

        for it in items:
            cur.execute("""
                INSERT INTO order_items
                (order_id, card_name, price, post_message_id, qty)
                VALUES (?, ?, ?, ?, ?)
            """, (
                order_id,
                it["card_name"],
                it["price"],
                it["post_mid"],
                it["qty"]
            ))

    upsert_checkout(user_id, stage="awaiting_payment", invoice_no=invoice_no)

    pdf = build_invoice_pdf(
        invoice_no=invoice_no,
        delivery_method=method,
        cards_total_sgd=cards_total,
        delivery_fee_sgd=delivery_fee,
        total_sgd=total,
        paynow_number=PAYNOW_NUMBER,
        paynow_name=PAYNOW_NAME
    )

    await cb.message.answer_document(
        BufferedInputFile(pdf, filename=f"{invoice_no}.pdf"),
        caption=(
            f"📄 Invoice Generated\n\n"
            f"Invoice: {invoice_no}\n"
            f"Total: ${total:.2f} SGD\n\n"
            "Please send payment proof screenshot."
        )
    )

    await cb.answer()

# ==========================================================
# PAYMENT PROOF + ADMIN APPROVAL
# ==========================================================

@router.message(F.chat.type == "private", (F.photo | F.document))
async def payment_proof_received(message: Message):

    ck = get_checkout(message.from_user.id) or {}

    if ck.get("stage") != "awaiting_payment":
        return

    invoice_no = ck.get("invoice_no")
    if not invoice_no:
        return

    # Update order status
    with get_db() as conn:
        conn.execute("""
            UPDATE orders
            SET status = 'payment_received'
            WHERE invoice_no = ?
              AND user_id = ?
        """, (invoice_no, message.from_user.id))

    upsert_checkout(message.from_user.id, stage="payment_submitted")

    # Notify buyer
    await message.answer(
        "✅ Payment proof received!\n\n"
        "⏳ Please wait for admin approval.\n\n"
        f"Invoice: {invoice_no}"
    )

    # Forward proof to admin
    try:
        await message.forward(chat_id=ADMIN_ID)

        admin_msg = (
            "📩 <b>New Payment Proof Received</b>\n\n"
            f"Invoice: <code>{invoice_no}</code>\n"
            f"User: @{message.from_user.username or 'NoUsername'}\n"
            f"User ID: <code>{message.from_user.id}</code>\n\n"
            "Approve with:\n"
            f"<code>/approve {invoice_no}</code>"
        )

        await message.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_msg,
            parse_mode="HTML"
        )

    except Exception as e:
        print("Error forwarding payment proof:", e)


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, Command("approve"))
async def admin_approve(message: Message):

    parts = (message.text or "").strip().split(maxsplit=1)

    if len(parts) < 2:
        await message.answer("❌ Usage: /approve <INVOICE_NO>")
        return

    invoice_no = parts[1].strip()

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT user_id, delivery_method, status
            FROM orders
            WHERE invoice_no = ?
        """, (invoice_no,))

        row = cur.fetchone()

        if not row:
            await message.answer("❌ Invoice not found.")
            return

        user_id = int(row["user_id"])
        delivery_method = row["delivery_method"]

        conn.execute("""
            UPDATE orders
            SET status = 'verifying'
            WHERE invoice_no = ?
        """, (invoice_no,))

    # If tracked mailing -> ask for address
    if delivery_method == "tracked":

        upsert_checkout(user_id, stage="awaiting_address", invoice_no=invoice_no)

        await message.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Payment proof received!\n\n"
                "📮 Next Step: Shipping Details\n"
                "Please copy and fill this template exactly and send it back:\n"
		"----------------------------------------------\n\n"
                "Name :\n"
                "Street Name :\n"
                "Unit Number :\n"
                "Postal Code :\n"
                "Phone Number :\n\n"
		"----------------------------------------------"
                f"Invoice: {invoice_no}"
            )
        )

        await message.answer(f"✅ Approved {invoice_no} (awaiting address)")

    else:
        # Self collection
        with get_db() as conn:
            conn.execute("""
                UPDATE orders
                SET status = 'ready_to_ship'
                WHERE invoice_no = ?
            """, (invoice_no,))

        upsert_checkout(user_id, stage="done", invoice_no=invoice_no)

        await message.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Payment verified!\n\n"
                "🏠 Self Collection selected.\n"
                f"📍 Pickup: {SELF_PICKUP_TEXT}\n\n"
                f"Invoice: {invoice_no}"
            )
        )

        await message.answer(f"✅ Approved {invoice_no} (self collection)")


# ==========================================================
# ADDRESS CAPTURE (STRICT FORMAT)
# ==========================================================

ADDRESS_RE = re.compile(
    r"Name\s*:\s*(?P<name>.+)\n"
    r"Street Name\s*:\s*(?P<street>.+)\n"
    r"Unit Number\s*:\s*(?P<unit>.+)\n"
    r"Postal Code\s*:\s*(?P<postal>.+)\n"
    r"Phone Number\s*:\s*(?P<phone>.+)",
    re.IGNORECASE
)


@router.message(F.chat.type == "private", F.text)
async def handle_address_text(message: Message):

    ck = get_checkout(message.from_user.id) or {}

    if ck.get("stage") != "awaiting_address":
        return

    invoice_no = ck.get("invoice_no")
    if not invoice_no:
        return

    m = ADDRESS_RE.search(message.text.strip())

    if not m:
        await message.answer(
            "❌ Format not detected. Please copy the template exactly:\n\n"
            "Name :\n"
            "Street Name :\n"
            "Unit Number :\n"
            "Postal Code :\n"
            "Phone Number :\n\n"
            f"Invoice: {invoice_no}"
        )
        return

    data = {
        "name": m.group("name").strip(),
        "street": m.group("street").strip(),
        "unit": m.group("unit").strip(),
        "postal": m.group("postal").strip(),
        "phone": m.group("phone").strip(),
    }

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT id FROM orders
            WHERE invoice_no = ?
              AND user_id = ?
        """, (invoice_no, message.from_user.id))

        order = cur.fetchone()

        if not order:
            await message.answer("❌ Order not found.")
            return

        order_id = int(order["id"])

        conn.execute("""
            INSERT INTO shipping_address
            (order_id, name, street_name, unit_number, postal_code, phone_number, confirmed)
            VALUES (?, ?, ?, ?, ?, ?, 0)
            ON CONFLICT(order_id) DO UPDATE SET
                name=excluded.name,
                street_name=excluded.street_name,
                unit_number=excluded.unit_number,
                postal_code=excluded.postal_code,
                phone_number=excluded.phone_number,
                confirmed=0
        """, (
            order_id,
            data["name"],
            data["street"],
            data["unit"],
            data["postal"],
            data["phone"]
        ))

    upsert_checkout(message.from_user.id, stage="confirm_address")

    await message.answer(
        "📮 Please confirm your delivery details:\n\n"
        f"Name : {data['name']}\n"
        f"Street Name : {data['street']}\n"
        f"Unit Number : {data['unit']}\n"
        f"Postal Code : {data['postal']}\n"
        f"Phone Number : {data['phone']}\n\n"
        "Are you sure these are the confirmed delivery details?",
        reply_markup=kb_confirm_address()
    )


@router.callback_query(F.data.startswith("addr:"))
async def addr_confirm(cb: CallbackQuery):

    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    if ck.get("stage") != "confirm_address":
        await cb.answer()
        return

    invoice_no = ck.get("invoice_no")
    if not invoice_no:
        await cb.answer()
        return

    action = cb.data.split(":", 1)[1]

    if action == "reenter":
        upsert_checkout(user_id, stage="awaiting_address")
        await cb.message.answer("✍️ Please re-enter your address using the template.")
        await cb.answer()
        return

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT id FROM orders
            WHERE invoice_no = ?
              AND user_id = ?
        """, (invoice_no, user_id))

        order = cur.fetchone()

        if not order:
            await cb.message.answer("❌ Order not found.")
            await cb.answer()
            return

        order_id = int(order["id"])

        conn.execute("UPDATE shipping_address SET confirmed = 1 WHERE order_id = ?", (order_id,))
        conn.execute("UPDATE orders SET status = 'ready_to_ship' WHERE id = ?", (order_id,))

        cur.execute("SELECT * FROM shipping_address WHERE order_id = ?", (order_id,))
        a = cur.fetchone()

    upsert_checkout(user_id, stage="done")

    await cb.message.answer(
        "✅ Shipping Address Confirmed!\n\n"
        f"Name : {a['name']}\n"
        f"Street Name : {a['street_name']}\n"
        f"Unit Number : {a['unit_number']}\n"
        f"Postal Code : {a['postal_code']}\n"
        f"Phone Number : {a['phone_number']}\n\n"
        "📋 Order Summary:\n"
        "• Payment proof: ✅ Received\n"
        "• Shipping address: ✅ Confirmed\n"
        "• Payment verification: 📦 Ready to Ship\n\n"
        "You will receive a tracking number once shipped.\n\n"
        f"Invoice: {invoice_no}"
    )

    await cb.answer()
