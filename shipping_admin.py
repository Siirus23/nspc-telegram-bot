# shipping_admin.py
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import ShippingActionCB, PackingActionCB

from ocr_utils import extract_text_from_photo, extract_tracking_number

import re

from db import (
    get_db,
    set_admin_session,
    get_admin_session,
    clear_admin_session,
    set_shipping_proof,
    get_shipping_proof,
    get_payment_proof,
    mark_order_packed,
    mark_order_shipped,
    STATUS_PACKING_PENDING,
    STATUS_PACKED, 
    STATUS_SHIPPED,
)

from config import ADMIN_ID, CHANNEL_ID

router = Router()

TRACKING_REGEX = re.compile(r"[A-Za-z]{2}\d{9}SG", re.IGNORECASE)
INVOICE_REGEX = re.compile(r"^INV-\d+$", re.IGNORECASE)


# ===========================
# ADMIN HELP TEXT
# ===========================

def admin_help_text():
    return """
🛠 <b>Admin Panel Guide</b>

🕒 Pending Payment Approvals
- Shows all payments awaiting approval

📦 Orders Ready To Pack
- Shows all paid & address-confirmed orders (READY TO SHIP)

🧾 Packing List
- Checklist of items to pack for READY TO SHIP orders

🚚 Orders Shipped
- Recently shipped orders list

⌨️ Type Tracking (OCR Fail)
- Manual fallback: type invoice first, then tracking

❌ Cancel Claims
- Admin wizard to remove buyer claims + restore stock

❌ Cancel Shipping Session
- Reset current admin workflow session
"""


# ===========================
# ADMIN PANEL
# ===========================

def build_admin_panel():
    kb = InlineKeyboardBuilder()

    kb.button(text="🕒 Pending Payment Approvals", callback_data="admin:pendingpay")
    kb.button(text="🧾 Packing List", callback_data="admin:packlist")
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
        "🛠 <b>Admin Control Panel</b>\n\nSelect an action:",
        parse_mode="HTML",
        reply_markup=build_admin_panel()
    )


# ===========================
# SHIPPED ORDERS LIST
# ===========================

async def list_shipped_orders(message: Message):
    with get_db() as conn:
        cur = conn.cursor()

        # SQL with a placeholder (?)
        cur.execute(
            """
            SELECT invoice_no, username, tracking_number
            FROM orders
            WHERE status = ?
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (STATUS_SHIPPED,)   # ← THIS IS THE “AND PASS” PART
        )

        rows = cur.fetchall()

    if not rows:
        await message.answer("📭 No shipped orders found.")
        return

    text = ["🚚 <b>Recently Shipped Orders</b>\n"]
    for r in rows:
        text.append(
            f"• <code>{r['invoice_no']}</code> – "
            f"@{r['username'] or 'NoUsername'} – "
            f"<code>{r['tracking_number']}</code>"
        )

    await message.answer("\n".join(text), parse_mode="HTML")


# ===========================
# ADMIN PANEL CALLBACKS
# ===========================

@router.callback_query(F.data.startswith("admin:"))
async def admin_panel_actions(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Unauthorized", show_alert=True)
        return

    action = cb.data.split(":", 1)[1]

    if  action == "packlist":
        await generate_packlist(cb.message)

    elif action == "toship":
        await show_orders_ready_to_ship(cb.message)
        
    elif action == "manual":
        # Manual fallback flow: invoice first, then tracking
        set_admin_session(ADMIN_ID, "awaiting_tracking_invoice", None)
        await cb.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "⌨️ <b>Type Tracking (OCR Fail)</b>\n\n"
                "Send the invoice number first.\n"
                "Example: <code>INV-000016</code>"
            ),
            parse_mode="HTML",
        )

    elif action == "cancelclaims":
        await list_cancel_claim_users(cb.message)

    elif action == "cancelship":
        clear_admin_session(ADMIN_ID)
        await cb.bot.send_message(
            chat_id=ADMIN_ID,
            text="✅ Shipping session cleared."
        )

    elif action == "help":
        await cb.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_help_text(),
            parse_mode="HTML"
        )

    elif action == "pendingpay":
        await list_pending_payments(cb.message)

    await cb.answer()


# ===========================
# CANCEL CLAIMS (ADMIN WIZARD)
# ===========================

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
async def list_pending_payments(message: Message):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT invoice_no, username, total
            FROM orders
            WHERE status = 'payment_received'
            ORDER BY created_at ASC
        """)
        rows = cur.fetchall()

    if not rows:
        await message.answer("✅ No payments awaiting approval.")
        return

    await message.answer("🕒 <b>Payments Awaiting Approval</b>", parse_mode="HTML")

    from callbacks import PaymentReviewCB

    for r in rows:
        inv = r["invoice_no"]
        user = r["username"] or "NoUsername"
        total = float(r["total"] or 0)

        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Approve", callback_data=PaymentReviewCB(action="approve", invoice=inv).pack())
        kb.button(text="❌ Reject", callback_data=PaymentReviewCB(action="reject", invoice=inv).pack())
        kb.adjust(2)

        text = (
            f"<b>Invoice:</b> <code>{inv}</code>\n"
            f"<b>Buyer:</b> @{user}\n"
            f"<b>Total:</b> ${total:.2f}\n"
            f"<b>Status:</b> PAYMENT RECEIVED"
        )

        proof_id, proof_type = get_payment_proof(inv)

        if proof_id and proof_type == "photo":
            await message.answer_photo(
                photo=proof_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        elif proof_id and proof_type == "document":
            await message.answer_document(
                document=proof_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )
        else:
            await message.answer(
                text + "\n\n⚠️ <b>No payment proof saved.</b>",
                parse_mode="HTML",
                reply_markup=kb.as_markup(),
            )

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, Command("pending"))
async def cmd_pending(message: Message):
    await list_pending_payments(message)

# ===========================
# PACKING LIST
# ===========================

@router.message(
    F.chat.type == "private",
    F.from_user.id == ADMIN_ID,
    Command("packlist")
)
async def generate_packlist(message: Message):
    with get_db() as conn:
        cur = conn.cursor()

        # 1️⃣ Fetch orders pending packing
        cur.execute("""
            SELECT id, invoice_no, username
            FROM orders
            WHERE status = ?
            ORDER BY created_at ASC
        """, (STATUS_PACKING_PENDING,))
        orders = cur.fetchall()

        if not orders:
            await message.answer("📦 No orders currently ready to pack.")
            return

        # 2️⃣ Send one message PER order
        for order in orders:
            order_id = order["id"]
            invoice_no = order["invoice_no"]
            username = order["username"] or "Unknown"

            text = f"📦 <b>Order to Pack</b>\n\n"
            text += f"<b>{invoice_no}</b> – @{username}\n\n"

            # 3️⃣ Fetch items for this order
            cur.execute("""
                SELECT card_name, qty
                FROM order_items
                WHERE order_id = ?
            """, (order_id,))
            items = cur.fetchall()

            for item in items:
                text += f"- {item['card_name']} (x{item['qty']})\n"

            # 4️⃣ Inline keyboard for THIS order
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="📦 Mark as Packed",
                            callback_data=PackingActionCB(
                                action="packed",
                                invoice=invoice_no
                            ).pack()

                        )
                    ]
                ]
            )

            # 5️⃣ Send message + button
            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=kb
            )

@router.callback_query(PackingActionCB.filter())
async def handle_packing_action(
    cb: CallbackQuery,
    callback_data: PackingActionCB
):
    invoice_no = callback_data.invoice
    action = callback_data.action

    # Safety check
    if action != "packed":
        await cb.answer("❌ Invalid packing action", show_alert=True)
        return

    # 1️⃣ Update DB
    with get_db() as conn:
        mark_order_packed(conn, invoice_no)

        cur = conn.execute(
            "SELECT user_id FROM orders WHERE invoice_no = ?",
            (invoice_no,)
        )
        row = cur.fetchone()

    # 2️⃣ Acknowledge admin
    await cb.answer("📦 Order marked as packed")

    # 3️⃣ Notify buyer
    if row:
        await cb.bot.send_message(
            row["user_id"],
            "📦 <b>Your order has been packed!</b>\n\n"
            "We’ll notify you once it ships.",
            parse_mode="HTML"
        )

    # 4️⃣ Update admin message (optional)
    try:
        await cb.message.edit_text(
            cb.message.text + "\n\n✅ <b>Status:</b> Packed",
            parse_mode="HTML"
        )
    except Exception:
        pass

# ===========================
# ORDERS READY TO SHIP
# ===========================

@router.message(
    F.chat.type == "private",
    F.from_user.id == ADMIN_ID,
    Command("readytoship")
)
async def show_orders_ready_to_ship(message: Message):
    with get_db() as conn:
        cur = conn.cursor()

        # 1️⃣ Fetch packed orders
        cur.execute("""
            SELECT id, invoice_no, username
            FROM orders
            WHERE status = ?
            ORDER BY created_at ASC
        """, (STATUS_PACKED,))
        orders = cur.fetchall()

        if not orders:
            await message.answer("🚚 No orders currently ready to ship.")
            return

        # 2️⃣ One message per order
        for order in orders:
            order_id = order["id"]
            invoice_no = order["invoice_no"]
            username = order["username"] or "Unknown"

            text = "🚚 <b>Order Ready To Ship</b>\n\n"
            text += f"<b>{invoice_no}</b> – @{username}\n\n"

            # 3️⃣ Fetch items
            cur.execute("""
                SELECT card_name, qty
                FROM order_items
                WHERE order_id = ?
            """, (order_id,))
            items = cur.fetchall()

            for item in items:
                text += f"- {item['card_name']} (x{item['qty']})\n"

            # 4️⃣ Button: Mark as Shipped
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🚚 Mark as Shipped",
                            callback_data=ShippingActionCB(
                                action="start",
                                invoice=invoice_no
                            ).pack()
                        )
                    ]
                ]
            )

            # 5️⃣ Send
            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=kb
            )

# ===========================
# SHIPPING LABEL PHOTO (STEP 3C)
# ===========================

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.photo)
async def admin_shipping_photo(message: Message):
    # 1️⃣ Check active shipping session
    sess = get_admin_session(ADMIN_ID)
    if not sess or sess.get("session_type") != "awaiting_tracking":
        await message.answer("⚠️ No active shipping session.")
        return

    invoice_no = sess.get("invoice_no")

    # 2️⃣ Save proof-of-shipping photo
    proof_file_id = message.photo[-1].file_id
    set_shipping_proof(invoice_no, proof_file_id)

    # 3️⃣ OCR the image
    text = await extract_text_from_photo(message.bot, message)

    if not text:
        await message.answer(
            "⚠️ Could not read text from image.\n\n"
            "Please type the tracking number manually (e.g. RR123456789SG)."
        )
        return

    # 4️⃣ Extract tracking number from OCR text
    tracking = extract_tracking_number(text)

    if not tracking:
        await message.answer(
            "❌ Tracking number not detected.\n\n"
            "Please type the tracking number manually (e.g. RR123456789SG)."
        )
        return

    # 5️⃣ Autofill tracking into text handler
    message.text = tracking
    await process_tracking_text(message)


# ===========================
# START SHIPPING SESSION
# ===========================

@router.callback_query(ShippingActionCB.filter(F.action == "start"))
async def start_shipping_button(cb: CallbackQuery, callback_data: ShippingActionCB):
    invoice_no = callback_data.invoice
    set_admin_session(ADMIN_ID, "awaiting_tracking", invoice_no)

    await cb.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📦 <b>Shipping started</b>\n\n"
            f"Invoice: <code>{invoice_no}</code>\n\n"
            "📸 Please upload the shipping label / barcode photo.\n"
            "⌨️ If OCR fails, you may type the tracking number manually."
        ),
        parse_mode="HTML"
    )
    await cb.answer()


# ===========================
# TRACKING HANDLERS
# ===========================

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.photo)
async def admin_tracking_photo(message: Message):
    sess = get_admin_session(ADMIN_ID)
    if not sess or sess.get("session_type") != "awaiting_tracking":
        await message.answer("⚠️ No active shipping session.")
        return


    # Save proof-of-shipping photo (always), even if OCR fails/unavailable
    invoice_no = sess.get("invoice_no")
    if invoice_no and message.photo:
        proof_file_id = message.photo[-1].file_id
        set_shipping_proof(invoice_no, proof_file_id)

    text = await extract_text_from_photo(message.bot, message)

    if not text:
        await message.answer(
            "⚠️ OCR is disabled/unavailable on this server.\n"
            "Please type the tracking number manually (e.g. RR123456789SG)."
        )
        return

    tracking = extract_tracking_number(text)
    if not tracking:
        await message.answer(
            "❌ Could not detect tracking from image.\n"
            "Please type the tracking number manually (e.g. RR123456789SG)."
        )
        return

    message.text = tracking
    await process_tracking_text(message)


async def process_manual_tracking_invoice_text(message: Message) -> bool:
    sess = get_admin_session(ADMIN_ID)
    if not sess or sess.get("session_type") != "awaiting_tracking_invoice":
        return False

    inv = (message.text or "").strip().upper()
    if not INVOICE_REGEX.match(inv):
        await message.answer("❌ Invalid invoice format. Example: <code>INV-000016</code>", parse_mode="HTML")
        return True

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT invoice_no, status FROM orders WHERE invoice_no = ?", (inv,))
        row = cur.fetchone()

    if not row:
        await message.answer("❌ Invoice not found. Try again.")
        return True

    if row["status"] != STATUS_PACKED:
        await message.answer(
            "⚠️ This invoice is not <b>READY TO SHIP</b>.\n"
            f"Current status: <code>{row['status']}</code>",
            parse_mode="HTML",
        )
        return True

    set_admin_session(ADMIN_ID, "awaiting_tracking", inv)
    await message.answer(
        f"✅ Invoice locked: <code>{inv}</code>\n\nNow type tracking (e.g. <code>RR123456789SG</code>).",
        parse_mode="HTML",
    )
    return True


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.text, ~F.text.startswith("/"))
async def admin_tracking_catcher(message: Message):
    handled = await process_manual_tracking_invoice_text(message)
    if handled:
        return

    handled = await process_tracking_text(message)
    if handled:
        return

    handled = await process_cancel_claims_text(message)
    if handled:
        return


async def process_tracking_text(message: Message) -> bool:
    sess = get_admin_session(ADMIN_ID)
    if not sess or sess.get("session_type") != "awaiting_tracking":
        return False

    invoice_no = sess.get("invoice_no")

    m = TRACKING_REGEX.search((message.text or "").upper())
    if not m:
        await message.answer("❌ Invalid tracking format.")
        return True

    tracking = m.group(0)

    from db import mark_order_shipped

    with get_db() as conn:
        # Fetch buyer BEFORE shipping
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM orders WHERE invoice_no = ?", (invoice_no,))
        row = cur.fetchone()

        # Use DB helper (single source of truth)
        mark_order_shipped(
            conn,
            invoice_no=invoice_no,
            tracking_number=tracking,
            proof_file_id=get_shipping_proof(invoice_no)
        )

    clear_admin_session(ADMIN_ID)

    await message.answer(f"✅ Tracking saved for <code>{invoice_no}</code>", parse_mode="HTML")

    if row:

        proof_file_id = get_shipping_proof(invoice_no)
        if proof_file_id:
            try:
                await message.bot.send_photo(
                    chat_id=row["user_id"],
                    photo=proof_file_id,
                    caption="📦 Proof of shipping",
                )
            except Exception:
                # If photo fails (e.g. file_id invalid), still send tracking text
                pass

        await message.bot.send_message(
            chat_id=row["user_id"],
            text=(
                "📦 <b>Your Order Has Been Shipped!</b>\n\n"
                f"<b>Invoice:</b> {invoice_no}\n"
                f"<b>Tracking Number:</b> {tracking}\n\n"
                "📍 <b>Track here:</b>\n"
                f"https://www.singpost.com/track-items?trackNums={tracking}\n\n"
                "Thank you for your purchase! 😊"
            ),
            parse_mode="HTML"
        )

    return True
