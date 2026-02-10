# shipping_admin.py
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import re

from callbacks import (
    ShippingActionCB,
    PackingActionCB,
    PaymentReviewCB,
)

from ocr_utils import extract_text_from_photo, extract_tracking_number

from db import (
    get_pool,
    get_orders_by_status,
    create_shipping_session,
    get_active_shipping_session,
    update_shipping_session,
    complete_shipping_session,
    mark_order_shipped,
    STATUS_PACKED,
)

from config import ADMIN_ID, CHANNEL_ID

router = Router()

TRACKING_REGEX = re.compile(r"[A-Za-z]{2}\d{9}SG", re.IGNORECASE)
INVOICE_REGEX = re.compile(r"^INV-\d+$", re.IGNORECASE)

# ======================================================
# ADMIN HELP TEXT
# ======================================================

def admin_help_text():
    return """
🛠 <b>Admin Panel Guide</b>

🕒 Pending Payment Approvals
📦 Packing List (mark packed)
🚚 Orders Ready To Ship
✅ Orders Shipped
⌨️ Type Tracking (OCR Fail)
❌ Cancel Claims (admin wizard)
❌ Cancel Shipping Session
"""

# ======================================================
# ADMIN PANEL UI
# ======================================================

def build_admin_panel():
    kb = InlineKeyboardBuilder()

    kb.button(text="🕒 Pending Payment Approvals", callback_data="admin:pendingpay")
    kb.button(text="📦 Packing List", callback_data="admin:packlist")
    kb.button(text="🚚 Orders Ready To Ship", callback_data="admin:toship")
    kb.button(text="✅ Orders Shipped", callback_data="admin:shipped")
    kb.button(text="⌨️ Type Tracking (OCR Fail)", callback_data="admin:manual")
    kb.button(text="❌ Cancel Claims", callback_data="admin:cancelclaims")
    kb.button(text="❌ Cancel Shipping Session", callback_data="admin:cancelship")
    kb.button(text="ℹ️ Admin Help", callback_data="admin:help")

    kb.adjust(1)
    return kb.as_markup()

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, Command("adminpanel"))
async def show_admin_panel(message: Message):
    await message.answer(
        "🛠 <b>Admin Control Panel</b>",
        parse_mode="HTML",
        reply_markup=build_admin_panel(),
    )

# ======================================================
# ADMIN PANEL ROUTER
# ======================================================

@router.callback_query(F.data.startswith("admin:"))
async def admin_panel_actions(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Unauthorized", show_alert=True)
        return

    action = cb.data.split(":", 1)[1]

    if action == "pendingpay":
        await list_pending_payments(cb.message)

    elif action == "packlist":
        await generate_packlist(cb.message)

    elif action == "toship":
        await show_orders_ready_to_ship(cb.message)

    elif action == "shipped":
        await list_shipped_orders(cb.message)

    elif action == "manual":
        set_admin_session(ADMIN_ID, "awaiting_tracking_invoice", None)
        await cb.bot.send_message(
            ADMIN_ID,
            "⌨️ <b>Manual Tracking</b>\n\nSend invoice number first.",
            parse_mode="HTML",
        )

    elif action == "cancelclaims":
        await list_cancel_claim_users(cb.message)

    elif action == "cancelship":
        clear_admin_session(ADMIN_ID)
        await cb.bot.send_message(ADMIN_ID, "✅ Shipping session cleared.")

    elif action == "help":
        await cb.bot.send_message(ADMIN_ID, admin_help_text(), parse_mode="HTML")

    await cb.answer()

# ======================================================
# PAYMENT REVIEW
# ======================================================

async def list_pending_payments(message: Message):
    pool = await get_pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT invoice_no, username, total
            FROM orders
            WHERE status = 'payment_received'
            ORDER BY created_at ASC
        """)

    if not rows:
        await message.answer("✅ No payments awaiting approval.")
        return

    for r in rows:
        invoice_no = r["invoice_no"]
        username = r["username"] or "NoUsername"
        total = float(r["total"] or 0)

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

        proof_id, proof_type = get_payment_proof(invoice_no)

        caption = (
            f"<b>Invoice:</b> <code>{invoice_no}</code>\n"
            f"<b>Buyer:</b> @{username}\n"
            f"<b>Total:</b> ${total:.2f}"
        )

        if proof_id and proof_type == "photo":
            await message.answer_photo(
                proof_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        elif proof_id and proof_type == "document":
            await message.answer_document(
                proof_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )

@router.callback_query(PaymentReviewCB.filter(F.action == "approve"))
async def handle_payment_approve(cb: CallbackQuery, callback_data: PaymentReviewCB):
    invoice_no = callback_data.invoice

    with get_db() as conn:
        order = conn.execute("""
            SELECT user_id, delivery_method
            FROM orders
            WHERE invoice_no = ?
              AND status = 'payment_received'
        """, (invoice_no,)).fetchone()

        if not order:
            await cb.answer("❌ Order already handled or not found.", show_alert=True)
            return

        user_id = order["user_id"]
        delivery_method = order["delivery_method"]

        # Lock order
        conn.execute("""
            UPDATE orders
            SET status = 'paid'
            WHERE invoice_no = ?
        """, (invoice_no,))

    await cb.answer("✅ Payment approved")

    # 🔀 SINGLE SOURCE OF TRUTH
    if delivery_method == "tracked":
        upsert_checkout(user_id, stage="awaiting_address")

        await cb.bot.send_message(
            user_id,
            "✅ <b>Payment verified!</b>\n\n"
            "📦 Please send your shipping address using this template:\n\n"
            f"<code>{address_template()}</code>",
            parse_mode="HTML"
        )

    else:  # self collection
        upsert_checkout(user_id, stage="packing")

        await cb.bot.send_message(
            user_id,
            "✅ <b>Payment verified!</b>\n\n"
            "🏠 This order is marked for <b>self collection</b>.\n"
            "Admin will contact you to arrange pickup.",
            parse_mode="HTML"
        )

        await cb.bot.send_message(
            ADMIN_ID,
            f"📦 <b>Order ready to pack</b>\n\nInvoice: <code>{invoice_no}</code>",
            parse_mode="HTML"
        )


# ======================================================
# PACKING LIST
# ======================================================

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, Command("packlist"))
async def generate_packlist(message: Message):
    pool = await get_pool()

    async with pool.acquire() as conn:
        orders = await conn.fetch("""
            SELECT id, invoice_no, username
            FROM orders
            WHERE status = 'packing'
            ORDER BY created_at ASC
        """)

        if not orders:
            await message.answer("📦 No orders to pack.")
            return

        for o in orders:
            items = await conn.fetch("""
                SELECT card_name, qty
                FROM order_items
                WHERE order_id = $1
            """, o["id"])

            text = f"📦 <b>{o['invoice_no']}</b> – @{o['username'] or 'Unknown'}\n\n"
            for it in items:
                text += f"- {it['card_name']} (x{it['qty']})\n"

            kb = InlineKeyboardBuilder()
            kb.button(
                text="📦 Mark as Packed",
                callback_data=PackingActionCB(
                    action="packed",
                    invoice=o["invoice_no"]
                ).pack()
            )

            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=kb.as_markup()
            )


@router.callback_query(PackingActionCB.filter())
async def handle_packing_action(cb: CallbackQuery, callback_data: PackingActionCB):
    pool = await get_pool()
    invoice_no = callback_data.invoice

    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE orders
            SET status = 'packed'
            WHERE invoice_no = $1
            RETURNING user_id
        """, invoice_no)

    await cb.answer("📦 Marked as packed")

    if row:
        await cb.bot.send_message(
            row["user_id"],
            "📦 <b>Your order has been packed.</b>",
            parse_mode="HTML"
        )



# ======================================================
# READY TO SHIP
# ======================================================

async def show_orders_ready_to_ship(message: Message):
    orders = await get_orders_by_status(STATUS_PACKED)

    if not orders:
        await message.answer("🚚 No orders ready to ship.")
        return

    for o in orders:
        kb = InlineKeyboardBuilder()
        kb.button(
            text="🚚 Mark as Shipped",
            callback_data=ShippingActionCB(
                action="start",
                invoice=o["invoice_no"]
            ).pack()
        )

        await message.answer(
            f"🚚 <b>{o['invoice_no']}</b> – @{o['username'] or 'Unknown'}",
            parse_mode="HTML",
            reply_markup=kb.as_markup()
        )

# ======================================================
# SHIPPING FLOW
# ======================================================

@router.callback_query(ShippingActionCB.filter(F.action == "start"))
async def start_shipping(cb: CallbackQuery, callback_data: ShippingActionCB):
    invoice_no = callback_data.invoice

    # Find the order that is packed and ready to ship
    orders = await get_orders_by_status(STATUS_PACKED)
    order = next((o for o in orders if o["invoice_no"] == invoice_no), None)

    if not order:
        await cb.answer("❌ Order not found or not ready to ship.", show_alert=True)
        return

    # Create a shipping session (crash-safe)
    await create_shipping_session(
        admin_id=cb.from_user.id,
        order_id=order["id"]
    )

    await cb.bot.send_message(
        cb.from_user.id,
        f"📦 Shipping started for <code>{invoice_no}</code>\n\n"
        "📸 Please upload the shipping label photo.",
        parse_mode="HTML"
    )

    await cb.answer()


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.photo)
async def admin_shipping_photo(message: Message):
    # Find active shipping session waiting for photo
    session = await get_active_shipping_session_by_admin(message.from_user.id)

    if not session:
        return  # No active shipping flow

    # Get the highest resolution photo
    photo_file_id = message.photo[-1].file_id

    # Save photo into the shipping session
    await update_shipping_session(
        order_id=session["order_id"],
        photo_file_id=photo_file_id,
        step="awaiting_confirmation"
    )

    await message.answer(
        "📸 Shipping photo received.\n\n"
        "⏳ Processing tracking number…",
        parse_mode="HTML"
    )

# ======================================================
# CANCEL CLAIMS WIZARD (VERBATIM — UNCHANGED)
# ======================================================

async def list_cancel_claim_users(message: Message):
    """Step 1: show numbered users who still have active claims."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id,
                   COALESCE(username, '') AS username,
                   COUNT(*) AS qty,
                   MIN(claimed_at) AS earliest
            FROM claims
            WHERE channel_chat_id = ?
              AND status = 'active'
            GROUP BY user_id
            ORDER BY earliest ASC
        """, (CHANNEL_ID,))
        rows = cur.fetchall()

    if not rows:
        await message.answer("✅ No active claims found.")
        return

    set_admin_session(ADMIN_ID, "cc_select_user", None)

    lines = [
        "❌ <b>Cancel Claims</b>",
        "Reply with a number to select a buyer (or <code>0</code> to exit):",
        ""
    ]

    for i, r in enumerate(rows, start=1):
        uname = f"@{r['username']}" if r["username"] else "(no username)"
        lines.append(f"{i}) {uname} — <code>{r['qty']}</code> claim(s) — <code>{r['user_id']}</code>")

    await message.answer("\n".join(lines), parse_mode="HTML")


def _fetch_nth_claim_user(n: int):
    """Recompute the same ordered list and return the nth user row."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT user_id,
                   COALESCE(username, '') AS username,
                   COUNT(*) AS qty,
                   MIN(claimed_at) AS earliest
            FROM claims
            WHERE channel_chat_id = ?
              AND status = 'active'
            GROUP BY user_id
            ORDER BY earliest ASC
        """, (CHANNEL_ID,))
        rows = cur.fetchall()

    if n < 1 or n > len(rows):
        return None
    return dict(rows[n - 1])


async def _send_user_claimed_cards(message: Message, user_id: int, username: str | None = None):
    """Step 2: show numbered cards (grouped) that this user has claimed."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                cl.card_name AS card_name,
                cl.price AS price_str,
                c.channel_message_id AS post_mid,
                COUNT(*) AS qty,
                MIN(c.claim_order) AS first_order
            FROM claims c
            JOIN card_listing cl
              ON cl.channel_chat_id = c.channel_chat_id
             AND cl.channel_message_id = c.channel_message_id
            WHERE c.channel_chat_id = ?
              AND c.user_id = ?
              AND c.status = 'active'
            GROUP BY cl.card_name, cl.price, c.channel_message_id
            ORDER BY first_order ASC
        """, (CHANNEL_ID, user_id))
        items = cur.fetchall()

    if not items:
        await message.answer("✅ This buyer has no active claims now.")
        set_admin_session(ADMIN_ID, "cc_select_user", None)
        return

    # store selected user_id in invoice_no column (text)
    set_admin_session(ADMIN_ID, "cc_select_items", str(user_id))

    uname = f"@{username}" if username else "(no username)"
    lines = [
        f"👤 <b>Buyer:</b> {uname} — <code>{user_id}</code>",
        "Reply with card number(s) to remove (e.g. <code>2</code> or <code>1 3 4</code>).",
        "Reply <code>0</code> to go back.",
        ""
    ]

    for i, it in enumerate(items, start=1):
        lines.append(
            f"{i}) {it['card_name']} — {it['price_str']} — x<code>{it['qty']}</code> — post <code>{it['post_mid']}</code>"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


def _fetch_user_claim_groups(user_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT
                cl.card_name AS card_name,
                cl.price AS price_str,
                c.channel_message_id AS post_mid,
                COUNT(*) AS qty,
                MIN(c.claim_order) AS first_order
            FROM claims c
            JOIN card_listing cl
              ON cl.channel_chat_id = c.channel_chat_id
             AND cl.channel_message_id = c.channel_message_id
            WHERE c.channel_chat_id = ?
              AND c.user_id = ?
              AND c.status = 'active'
            GROUP BY cl.card_name, cl.price, c.channel_message_id
            ORDER BY first_order ASC
        """, (CHANNEL_ID, user_id))
        return [dict(r) for r in cur.fetchall()]


async def _admin_cancel_claim_group(message: Message, user_id: int, post_mid: int, reason: str = "admin_cancel"):
    """
    Cancel ALL active claims for a user on a specific post (card), restore stock, update caption.
    Also adjusts any non-shipped order for that user.
    """
    with get_db() as conn:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        cur.execute("""
            SELECT COUNT(*) AS c
            FROM claims
            WHERE channel_chat_id = ?
              AND channel_message_id = ?
              AND user_id = ?
              AND status = 'active'
        """, (CHANNEL_ID, post_mid, user_id))
        qty = cur.fetchone()["c"]

        if qty <= 0:
            conn.rollback()
            return None

        cur.execute("""
            SELECT card_name, price, remaining_qty
            FROM card_listing
            WHERE channel_chat_id = ?
              AND channel_message_id = ?
        """, (CHANNEL_ID, post_mid))
        card = cur.fetchone()

        if not card:
            conn.rollback()
            return None

        card_name = card["card_name"]
        price_str = card["price"]
        remaining = int(card["remaining_qty"])

        cur.execute("""
            UPDATE claims
            SET status = 'cancelled'
            WHERE channel_chat_id = ?
              AND channel_message_id = ?
              AND user_id = ?
              AND status = 'active'
        """, (CHANNEL_ID, post_mid, user_id))

        cur.execute("""
            UPDATE card_listing
            SET remaining_qty = remaining_qty + ?
            WHERE channel_chat_id = ?
              AND channel_message_id = ?
        """, (qty, CHANNEL_ID, post_mid))

        cur.execute("""
            INSERT INTO admin_logs (action_type, admin_id, target_user_id, card_name, channel_message_id, quantity, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("cancel_claim", ADMIN_ID, user_id, card_name, post_mid, qty, reason))

        conn.commit()

    new_remaining = remaining + qty

    caption = (
        f"{card_name}\nPrice: {price_str}\n❌ SOLD OUT"
        if new_remaining <= 0 else
        f"{card_name}\nPrice: {price_str}\nAvailable: {new_remaining}"
    )
    try:
        await message.bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=post_mid, caption=caption)
    except Exception:
        pass

    order_cancelled = False
    updated_invoice = None

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, invoice_no, delivery_fee, status
            FROM orders
            WHERE user_id = ?
              AND status IN ('pending_payment', 'payment_received', 'verifying', 'ready_to_ship')
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id,))
        ord_row = cur.fetchone()

        if ord_row:
            order_id = ord_row["id"]
            updated_invoice = ord_row["invoice_no"]

            cur.execute("""
                SELECT id, qty, price
                FROM order_items
                WHERE order_id = ?
                  AND post_message_id = ?
            """, (order_id, post_mid))
            oi = cur.fetchone()

            if oi:
                oi_id = oi["id"]
                oi_qty = int(oi["qty"])

                remove_qty = min(qty, oi_qty)
                new_qty = oi_qty - remove_qty

                if new_qty <= 0:
                    cur.execute("DELETE FROM order_items WHERE id = ?", (oi_id,))
                else:
                    cur.execute("UPDATE order_items SET qty = ? WHERE id = ?", (new_qty, oi_id))

                cur.execute("""
                    SELECT COALESCE(SUM(price * qty), 0) AS cards_total
                    FROM order_items
                    WHERE order_id = ?
                """, (order_id,))
                cards_total = float(cur.fetchone()["cards_total"] or 0)

                delivery_fee = float(ord_row["delivery_fee"] or 0)
                total = cards_total + delivery_fee

                if cards_total <= 0:
                    cur.execute(
                        "UPDATE orders SET status = 'cancelled', cards_total = 0, total = 0 WHERE id = ?",
                        (order_id,)
                    )
                    order_cancelled = True
                else:
                    cur.execute(
                        "UPDATE orders SET cards_total = ?, total = ? WHERE id = ?",
                        (cards_total, total, order_id)
                    )

            conn.commit()

    return {
        "card_name": card_name,
        "qty": qty,
        "post_mid": post_mid,
        "new_remaining": new_remaining,
        "invoice_no": updated_invoice,
        "order_cancelled": order_cancelled,
    }


def _parse_selection_numbers(text: str):
    parts = text.replace(",", " ").split()
    nums = []
    for p in parts:
        if p.isdigit():
            nums.append(int(p))
    return sorted(set(nums))


async def process_cancel_claims_text(message: Message) -> bool:
    sess = get_admin_session(ADMIN_ID)
    if not sess:
        return False

    stype = sess.get("session_type")
    text = (message.text or "").strip()

    if stype == "cc_select_user":
        if text == "0":
            clear_admin_session(ADMIN_ID)
            await message.answer("✅ Cancel-claims exited.")
            return True

        if not text.isdigit():
            await message.answer("❌ Reply with a number (e.g. 1), or 0 to exit.")
            return True

        n = int(text)
        row = _fetch_nth_claim_user(n)
        if not row:
            await message.answer("❌ Invalid number. Try again.")
            return True

        # Move to item-selection view for this buyer
        await _send_user_claimed_cards(
            message,
            user_id=int(row["user_id"]),
            username=row.get("username") or None,
        )
        return True

    if stype == "cc_select_items":
        if text == "0":
            await list_cancel_claim_users(message)
            return True

        # NOTE: admin_sessions.invoice_no stores the selected buyer_id for this flow
        user_id = int(sess.get("invoice_no") or "0")
        if not user_id:
            await list_cancel_claim_users(message)
            return True

        groups = _fetch_user_claim_groups(user_id)
        if not groups:
            await message.answer("✅ No active claims left for this buyer.")
            await list_cancel_claim_users(message)
            return True

        nums = _parse_selection_numbers(text)
        if not nums:
            await message.answer("❌ Reply with card number(s), e.g. 2 or 1 3 4.")
            return True

        chosen = [groups[i - 1] for i in nums if 1 <= i <= len(groups)]
        if not chosen:
            await message.answer("❌ Invalid selection.")
            return True

        removed_lines = []
        order_cancelled_any = False
        invoice_touched = None

        for g in chosen:
            res = await _admin_cancel_claim_group(
                message,
                user_id=user_id,
                post_mid=int(g["post_mid"]),
            )
            if res:
                removed_lines.append(f"• {res['card_name']} x<code>{res['qty']}</code>")
                if res.get("order_cancelled"):
                    order_cancelled_any = True
                if res.get("invoice_no"):
                    invoice_touched = res["invoice_no"]

        if not removed_lines:
            await message.answer("⚠️ Nothing removed (claims may have changed).")
            await _send_user_claimed_cards(message, user_id=user_id, username=None)
            return True

        summary = ["✅ <b>Claims removed</b>", *removed_lines]
        if invoice_touched:
            summary.append(f"\n🧾 <b>Invoice updated:</b> <code>{invoice_touched}</code>")
        if order_cancelled_any:
            summary.append("⚠️ <b>Order auto-cancelled</b> (no items left).")

        await message.answer("\n".join(summary), parse_mode="HTML")

        await _send_user_claimed_cards(message, user_id=user_id, username=None)
        return True

    return False

@router.callback_query(PaymentReviewCB.filter(F.action == "approve"))
async def approve_payment(cb: CallbackQuery, callback_data: PaymentReviewCB):
    invoice_no = callback_data.invoice

    with get_db() as conn:
        # Fetch order
        order = conn.execute("""
            SELECT user_id, delivery_method
            FROM orders
            WHERE invoice_no = ?
              AND status = 'payment_received'
        """, (invoice_no,)).fetchone()

        if not order:
            await cb.answer("❌ Order not found or already processed.", show_alert=True)
            return

        user_id = order["user_id"]
        delivery_method = order["delivery_method"]

        # Lock order as paid
        conn.execute("""
            UPDATE orders
            SET status = 'paid'
            WHERE invoice_no = ?
        """, (invoice_no,))

    await cb.answer("✅ Payment approved")

    # 🔀 THIS IS THE MISSING FORK
    if delivery_method == "tracked":
        upsert_checkout(
            user_id,
            stage="awaiting_address"
        )

        await cb.bot.send_message(
            user_id,
            "✅ <b>Payment verified!</b>\n\n"
            "📦 Please send your shipping address using the template below:\n\n"
            f"<code>{address_template()}</code>",
            parse_mode="HTML"
        )

    else:  # self collection
        upsert_checkout(
            user_id,
            stage="packing"
        )

        await cb.bot.send_message(
            user_id,
            "✅ <b>Payment verified!</b>\n\n"
            "🏠 This order is marked for <b>self collection</b>.\n"
            "Admin will contact you to arrange pickup.",
            parse_mode="HTML"
        )

        # Notify admin immediately
        await cb.bot.send_message(
            ADMIN_ID,
            f"📦 <b>Order ready to pack</b>\n\nInvoice: <code>{invoice_no}</code>",
            parse_mode="HTML"
        )



# ======================================================
# TRACKING HANDLERS
# ======================================================

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.photo)
async def admin_shipping_photo(message: Message):
    # Find active shipping session waiting for a photo
    session = await get_active_shipping_session_by_admin(message.from_user.id)

    if not session:
        return  # Admin is not in a shipping flow

    # Take the highest resolution photo
    photo_file_id = message.photo[-1].file_id

    # Save photo and move session to next step
    await update_shipping_session(
        order_id=session["order_id"],
        photo_file_id=photo_file_id,
        step="awaiting_confirmation"
    )

    await message.answer(
        "📸 Shipping photo received.\n\n"
        "Next: tracking number confirmation.",
        parse_mode="HTML"
    )

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.text)
async def admin_shipping_tracking_text(message: Message):
    session = await get_active_shipping_session_by_admin(message.from_user.id)

    # We only care if we're waiting for confirmation step
    if not session or session["step"] != "awaiting_confirmation":
        return

    tracking = extract_tracking_number((message.text or "").upper())
    if not tracking:
        await message.answer("❌ Invalid tracking number format. Please try again.")
        return

    # Save tracking number into session
    await update_shipping_session(
        order_id=session["order_id"],
        detected_tracking=tracking
    )

    await message.answer(
        f"📦 Tracking detected:\n<code>{tracking}</code>\n\n"
        "Reply <b>CONFIRM</b> to mark this order as shipped.",
        parse_mode="HTML"
    )

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.text.casefold() == "confirm")
async def admin_confirm_shipping(message: Message):
    session = await get_active_shipping_session_by_admin(message.from_user.id)

    if not session or not session["detected_tracking"]:
        await message.answer("❌ No shipping session awaiting confirmation.")
        return

    # Mark order as shipped (final, atomic)
    await mark_order_shipped(
        order_id=session["order_id"],
        tracking=session["detected_tracking"],
        file_id=session["photo_file_id"]
    )

    # Close shipping session
    await complete_shipping_session(session["order_id"])

    await message.answer("✅ Order marked as shipped.")

    # Notify buyer
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT user_id FROM orders WHERE id = $1",
            session["order_id"]
        )

    if row:
        await message.bot.send_photo(
            chat_id=row["user_id"],
            photo=session["photo_file_id"],
            caption=(
                "📦 <b>Your order has been shipped!</b>\n"
                f"Tracking: <code>{session['detected_tracking']}</code>"
            ),
            parse_mode="HTML"
        )

