import re
from datetime import datetime
from typing import Optional
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import PaymentReviewCB
from aiogram.types import CallbackQuery
from config import ADMIN_ID, CHANNEL_ID, CHANNEL_USERNAME
from db import get_db
from invoice_pdf import build_invoice_pdf
from callbacks import PaymentReviewCB

router = Router()

# ====== CONFIG ======
PAYNOW_NUMBER = "93385994"
PAYNOW_NAME = "Naufal"

TRACKED_FEE_SGD = 3.50
SELF_PICKUP_TEXT = "806 Woodlands St 81, in front of Rainbow Mart"


# ====== HELPERS ======

async def show_available_cards(bot, user_id):
    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT channel_chat_id, channel_message_id, card_name, price, remaining_qty
            FROM card_listing
            WHERE remaining_qty > 0
            AND channel_message_id != 0
            ORDER BY id ASC
        """)

        cards = cur.fetchall()

    if not cards:
        await bot.send_message(
            chat_id=user_id,
            text="📭 No additional cards currently available."
        )
        return

    for c in cards:
        chat_id = c["channel_chat_id"]
        mid = c["channel_message_id"]
        name = c["card_name"]
        price = c["price"]
        remaining = c["remaining_qty"]

        # Build caption exactly like channel
        caption = f"{name}\nPrice: {price}\nAvailable: {remaining}"

        # Create link button
        link = make_post_link(chat_id, CHANNEL_USERNAME, mid)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Open Post in Channel", url=link)]
        ])

        try:
            # Forward the original post to the user
            await bot.forward_message(
                chat_id=user_id,
                from_chat_id=chat_id,
                message_id=mid
            )

            # Send our structured info under it
            await bot.send_message(
                chat_id=user_id,
                text="🔗 Open the post using the button below:",
                reply_markup=kb
            )


        except Exception as e:
            print("Error showing available card:", e)


def parse_price_to_float(price_str: str) -> float:
    s = (price_str or "").strip().upper()
    s = s.replace("SGD", "").replace("$", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def make_post_link(channel_chat_id: int, channel_username: str, post_mid: int) -> str:
    username = (channel_username or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}/{post_mid}"

    # fallback (private channel link format)
    s = str(abs(int(channel_chat_id)))
    internal = s[3:] if s.startswith("100") and len(s) > 3 else s
    return f"https://t.me/c/{internal}/{post_mid}"


def kb_delivery():
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Tracked Mail", callback_data="checkout:delivery:tracked")
    kb.button(text="🏠 Self Collection", callback_data="checkout:delivery:self")
    kb.button(text="🙋 Human Help", callback_data="checkout:delivery:human")
    kb.adjust(1)
    return kb.as_markup()


def kb_yes_no_browse():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yes", callback_data="checkout:browse:yes")
    kb.button(text="➡️ No, continue", callback_data="checkout:browse:no")
    kb.adjust(2)
    return kb.as_markup()


def kb_continue():
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Confirm Checkout", callback_data="checkout:continue")
    kb.adjust(1)
    return kb.as_markup()


def kb_confirm_address():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Yes, Confirm", callback_data="checkout:address:confirm")
    kb.button(text="❌ No, re-enter", callback_data="checkout:address:reenter")
    kb.adjust(2)
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
    "👋 <b>Welcome!</b>\n\n"
    "🎉 Let’s prepare your checkout.\n"
    "You will receive your invoice after confirming your delivery option.\n\n"
    "💡 After checkout, you can manage everything using:\n"
    "👉 <b>/buyerpanel</b>\n\n"
    "There you can:\n"
    "• View your orders\n"
    "• Resend invoices\n"
    "• Edit shipping address\n"
    "• Check your claims\n\n"
    "Thank you for your patience! 🙂",
    parse_mode="HTML"
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
        invoice_no=None,
        delivery_method=None,
    )

    await message.answer(
        summary_text +
        "\n\n📦 <b>Choose your delivery method:</b>\n"
        f"• Tracked Mail: +${TRACKED_FEE_SGD:.2f} delivery charge\n"
        "• Self Collection: No extra charge\n"
        f"📍 Pickup: {SELF_PICKUP_TEXT}\n\n"
        "Please select your preferred option below:\n\n"
        "🧾 <i>Invoice will be generated only after you confirm checkout.</i>",
        parse_mode="HTML",
        reply_markup=kb_delivery()
    )


@router.callback_query(F.data.startswith("checkout:delivery:"))
async def delivery_pick(cb: CallbackQuery):
    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    if ck.get("stage") != "choose_delivery":
        await cb.answer()
        return

    parts = cb.data.split(":")
    choice = parts[2] if len(parts) >= 3 else ""

    if choice == "human":
        await cb.message.answer("🙋 Please DM the admin for help.")
        await cb.answer()
        return

    if choice == "tracked":
        delivery_fee = TRACKED_FEE_SGD
        method = "tracked"
    elif choice == "self":
        delivery_fee = 0
        method = "self"
    else:
        await cb.answer("Invalid option", show_alert=True)
        return

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


@router.callback_query(F.data.startswith("checkout:browse:"))
async def browse_decision(cb: CallbackQuery):
    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    # keep stage as awaiting_browse until invoice is generated
    if ck.get("stage") != "awaiting_browse":
        await cb.answer()
        return

    choice = cb.data.split(":")[2]

    if choice == "yes":
        await cb.message.answer("🔍 Showing currently available cards...")

        await show_available_cards(
            bot=cb.message.bot,
            user_id=user_id
        )

        await cb.message.answer(
            "When you're ready to continue with your invoice:",
            reply_markup=kb_continue()
        )
    else:
        await cb.message.answer("🧾 Continuing to invoice generation.🧾", reply_markup=kb_continue())

    await cb.answer()


@router.callback_query(F.data == "checkout:continue")
async def checkout_continue(cb: CallbackQuery):
    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    # must still be in awaiting_browse to avoid old button presses
    if ck.get("stage") != "awaiting_browse":
        await cb.answer("This button is no longer valid.", show_alert=True)
        return

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

    _, cards_total = format_claim_summary(items)
    delivery_fee = float(ck.get("delivery_fee") or 0)
    total = cards_total + delivery_fee

    # ---- Create order FIRST, then derive invoice from order_id (race-safe) ----
    with get_db() as conn:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        # Insert with NULL invoice_no (allowed by schema update in db.py)
        cur.execute("""
            INSERT INTO orders
            (invoice_no, user_id, username, delivery_method, cards_total, delivery_fee, total, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            None,
            user_id,
            cb.from_user.username or "",
            method,
            cards_total,
            delivery_fee,
            total,
            "pending_payment"
        ))

        order_id = cur.lastrowid
        invoice_no = f"INV-{order_id:06d}"

        cur.execute("UPDATE orders SET invoice_no = ? WHERE id = ?", (invoice_no, order_id))

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

    # ===== INVOICE PDF =====
    invoice_items = [{"name": it["card_name"], "qty": it["qty"], "price": it["price"]} for it in items]

    buyer_address = ""
    if method == "tracked":
        buyer_address = "Address will be provided by buyer after payment approval"

    pdf = build_invoice_pdf(
        invoice_no=invoice_no,
        delivery_method=method,
        cards_total_sgd=cards_total,
        delivery_fee_sgd=delivery_fee,
        total_sgd=total,
        paynow_number=PAYNOW_NUMBER,
        paynow_name=PAYNOW_NAME,
        buyer_username=cb.from_user.username or "",
        buyer_address=buyer_address,
        items=invoice_items
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

    # Merchant copy to admin
    try:
        await cb.message.bot.send_document(
            chat_id=ADMIN_ID,
            document=BufferedInputFile(pdf, filename=f"{invoice_no}_MERCHANT_COPY.pdf"),
            caption=(
                "🧾 <b>New Invoice Generated</b>\n\n"
                f"Invoice: <code>{invoice_no}</code>\n"
                f"Buyer: @{cb.from_user.username or 'NoUsername'}\n"
                f"User ID: <code>{user_id}</code>\n"
                f"Total: ${total:.2f} SGD\n"
                f"Delivery Method: {method.upper()}\n\n"
                "📌 Address: Pending buyer confirmation"
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        print("Failed to send merchant invoice copy:", e)

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

    await message.answer(
        "✅ Payment proof received!\n\n"
        "⏳ Please wait for admin approval.\n\n"
        f"Invoice: {invoice_no}"
    )

    try:
        # Build approve / reject keyboard
        from callbacks import PaymentReviewCB
        from aiogram.utils.keyboard import InlineKeyboardBuilder

        kb = InlineKeyboardBuilder()

        kb.button(
            text="✅ Approve",
            callback_data=PaymentReviewCB(action="approve", invoice=invoice_no).pack()
        )

        kb.button(
            text="❌ Reject",
            callback_data=PaymentReviewCB(action="reject", invoice=invoice_no).pack()
        )

        kb.adjust(2)

        admin_caption = (
            "📩 <b>New Payment Proof Received</b>\n\n"
            f"Invoice: <code>{invoice_no}</code>\n"
            f"User: @{message.from_user.username or 'NoUsername'}\n"
            f"User ID: <code>{message.from_user.id}</code>\n\n"
            "Please review this payment:"
        )

        # Send ONE combined message to admin
        if message.photo:
            await message.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=message.photo[-1].file_id,
                caption=admin_caption,
                parse_mode="HTML",
                reply_markup=kb.as_markup()
            )

        elif message.document:
            await message.bot.send_document(
                chat_id=ADMIN_ID,
                document=message.document.file_id,
                caption=admin_caption,
                parse_mode="HTML",
                reply_markup=kb.as_markup()
            )

    except Exception as e:
        print("Error sending payment proof to admin:", e)


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

    if delivery_method == "tracked":
        upsert_checkout(user_id, stage="awaiting_address", invoice_no=invoice_no)

        await message.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Payment verified!\n\n"
                "📮 Next Step: Shipping Details\n"
                "Please copy and fill this template exactly and send it back:\n"
                "----------------------------------------------\n"
                "Name :\n"
                "Street Name :\n"
                "Unit Number :\n"
                "Postal Code :\n"
                "Phone Number :\n"
                "----------------------------------------------\n"
                f"Invoice: {invoice_no}"
            )
        )

        await message.answer(f"✅ Approved {invoice_no} (awaiting address)")
        return

    # self collection
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
            "✅ <b>Payment Verified – Self Collection Confirmed!</b>\n\n"
            "📍 <b>Collection Location:</b>\n"
            f"{SELF_PICKUP_TEXT}\n\n"
            "⏰ <b>Collection Time:</b>\n"
            "• Please arrange a time with the seller via Telegram DM\n"
            "• Self-collection is strictly by appointment only\n\n"
            "📦 <b>What to Bring:</b>\n"
            "• Show your invoice upon arrival\n\n"
            "⚠️ <b>Important Notes:</b>\n"
            "• Orders must be collected within 7 days\n"
            "• Uncollected orders after 7 days may be cancelled\n"
            "• Please inspect items on the spot during collection\n\n"
            f"🧾 <b>Invoice:</b> {invoice_no}\n\n"
            "Thank you! Please message the seller to arrange pickup 😊"
        ),
        parse_mode="HTML"
    )

    await message.answer(f"✅ Approved {invoice_no} (self collection)")

@router.callback_query(PaymentReviewCB.filter(F.action == "approve"))
async def approve_via_button(cb: CallbackQuery, callback_data: PaymentReviewCB):

    invoice_no = callback_data.invoice

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id, delivery_method, status
            FROM orders
            WHERE invoice_no = ?
        """, (invoice_no,))
        row = cur.fetchone()

        if not row:
            await cb.answer("Invoice not found", show_alert=True)
            return

        user_id = int(row["user_id"])
        delivery_method = row["delivery_method"]

        conn.execute("""
            UPDATE orders
            SET status = 'verifying'
            WHERE invoice_no = ?
        """, (invoice_no,))

    # Now replicate the same logic your admin_approve command does

    if delivery_method == "tracked":
        upsert_checkout(user_id, stage="awaiting_address", invoice_no=invoice_no)

        await cb.message.bot.send_message(
            chat_id=user_id,
            text=(
                "✅ Payment verified!\n\n"
                "📮 Next Step: Shipping Details\n"
                "Please copy and fill this template exactly and send it back:\n"
                "----------------------------------------------\n"
                "Name :\n"
                "Street Name :\n"
                "Unit Number :\n"
                "Postal Code :\n"
                "Phone Number :\n"
                "----------------------------------------------\n"
                f"Invoice: {invoice_no}"
            )
        )

        await cb.message.answer(f"✅ Approved {invoice_no} (awaiting address)")
        await cb.answer("Approved")
        return

    # Self collection flow
    with get_db() as conn:
        conn.execute("""
            UPDATE orders
            SET status = 'ready_to_ship'
            WHERE invoice_no = ?
        """, (invoice_no,))

    upsert_checkout(user_id, stage="done", invoice_no=invoice_no)

    await cb.message.bot.send_message(
        chat_id=user_id,
        text=(
            "✅ <b>Payment Verified – Self Collection Confirmed!</b>\n\n"
            "📍 <b>Collection Location:</b>\n"
            f"{SELF_PICKUP_TEXT}\n\n"
            "Please message the seller to arrange pickup 😊\n\n"
            f"🧾 <b>Invoice:</b> {invoice_no}"
        ),
        parse_mode="HTML"
    )

    await cb.message.answer(f"✅ Approved {invoice_no}")
    await cb.answer("Approved")


@router.callback_query(PaymentReviewCB.filter(F.action == "reject"))
async def reject_payment(cb: CallbackQuery, callback_data: PaymentReviewCB):

    invoice_no = callback_data.invoice

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
            SELECT user_id FROM orders WHERE invoice_no = ?
        """, (invoice_no,))
        row = cur.fetchone()

        if not row:
            await cb.answer("Invoice not found", show_alert=True)
            return

        user_id = row["user_id"]

        conn.execute("""
            UPDATE orders
            SET status = 'rejected'
            WHERE invoice_no = ?
        """, (invoice_no,))

    upsert_checkout(user_id, stage="awaiting_payment")

    await cb.message.bot.send_message(
        chat_id=user_id,
        text=(
            "❌ <b>Payment Proof Rejected</b>\n\n"
            f"Invoice: {invoice_no}\n\n"
            "Please re-submit a clearer payment screenshot."
        ),
        parse_mode="HTML"
    )

    await cb.message.answer(f"❌ Rejected {invoice_no}")
    await cb.answer("Rejected")


# ==========================================================
# ADDRESS CAPTURE (handled by central dispatcher)
# ==========================================================

ADDRESS_RE = re.compile(
    r"Name\s*:\s*(?P<name>.+)\n"
    r"Street Name\s*:\s*(?P<street>.+)\n"
    r"Unit Number\s*:\s*(?P<unit>.+)\n"
    r"Postal Code\s*:\s*(?P<postal>.+)\n"
    r"Phone Number\s*:\s*(?P<phone>.+)",
    re.IGNORECASE
)


async def process_address_text(message: Message) -> bool:
    ck = get_checkout(message.from_user.id) or {}
    if ck.get("stage") != "awaiting_address":
        return False

    invoice_no = ck.get("invoice_no")
    if not invoice_no:
        return True

    m = ADDRESS_RE.search((message.text or "").strip())
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
        return True

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
            return True

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
    return True


@router.callback_query(F.data.startswith("checkout:address:"))
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

    action = cb.data.split(":")[2]

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

    # Notify Admin that order is ready to ship
    from callbacks import ShippingActionCB
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    kb = InlineKeyboardBuilder()

    kb.button(
        text="🚚 Start Shipping",
        callback_data=ShippingActionCB(action="start", invoice=invoice_no).pack()
    )

    kb.button(
        text="❌ Cancel Order",
        callback_data=ShippingActionCB(action="cancel", invoice=invoice_no).pack()
    )

    kb.adjust(2)

    await cb.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📦 <b>ORDER READY TO SHIP</b>\n\n"
            f"Invoice: <code>{invoice_no}</code>\n"
            f"Buyer: @{cb.from_user.username or 'NoUsername'}\n"
            f"User ID: <code>{user_id}</code>\n\n"
            "Status: <b>READY TO SHIP</b>\n\n"
            "Choose an action:"
        ),
        parse_mode="HTML",
        reply_markup=kb.as_markup()
    )



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