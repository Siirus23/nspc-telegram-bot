from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from callbacks import ShippingActionCB
from ocr_utils import extract_text_from_photo, extract_tracking_number

import re
import csv
import io

from db import (
    get_db,
    set_admin_session,
    get_admin_session,
    clear_admin_session
)

from config import ADMIN_ID, CHANNEL_ID

router = Router()

TRACKING_REGEX = re.compile(r"[A-Za-z]{2}\d{9}SG", re.IGNORECASE)


# ===========================
# ADMIN HELP TEXT
# ===========================

def admin_help_text():
    return """
🛠 <b>Admin Panel Guide</b>

📦 Orders Ready To Ship  
- Shows all paid & address-confirmed orders  

🧾 Packing List  
- Checklist of items to pack  

🚚 View Shipped Orders  
- Recently shipped orders  

📮 Manual Tracking Entry  
- Enter tracking manually if needed  

❌ Cancel Shipping Session  
- Reset current workflow  
"""


# ===========================
# ADMIN PANEL
# ===========================

def build_admin_panel():
    kb = InlineKeyboardBuilder()

    kb.button(text="🕒 Pending Payment Approvals", callback_data="admin:pendingpay")
    kb.button(text="🧾 Packing List", callback_data="admin:packlist")
    kb.button(text="📦 Orders Ready To Ship", callback_data="admin:toship")
    kb.button(text="🚚 View Shipped Orders", callback_data="admin:shipped")
    kb.button(text="📮 Manual Tracking Entry", callback_data="admin:manual")
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
        cur.execute("""
            SELECT invoice_no, username, tracking_number
            FROM orders
            WHERE status = 'shipped'
            ORDER BY created_at DESC
            LIMIT 20
        """)
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

    if action == "toship":
        await list_orders_ready(cb.message)

    elif action == "packlist":
        await generate_packlist(cb.message)

    elif action == "shipped":
        await list_shipped_orders(cb.message)

    elif action == "manual":
        set_admin_session(ADMIN_ID, "awaiting_tracking", None)
        await cb.bot.send_message(
            chat_id=ADMIN_ID,
            text="📮 Manual tracking mode activated. Send tracking number now.",
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

    # store selected user_id in invoice_no column (text) to persist across restart
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
    """Cancel ALL active claims for a user on a specific post (card), restore stock, update caption.
       Also adjusts any non-shipped order for that user (A + C integration).
    """
    # Step 1: cancel claims + restore inventory atomically
    with get_db() as conn:
        cur = conn.cursor()
        conn.execute("BEGIN IMMEDIATE")

        # how many active claims on this post for this user
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

        # restore inventory and get card info
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

        # admin log
        cur.execute("""
            INSERT INTO admin_logs (action_type, admin_id, target_user_id, card_name, channel_message_id, quantity, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("cancel_claim", ADMIN_ID, user_id, card_name, post_mid, qty, reason))

        conn.commit()

    new_remaining = remaining + qty

    # Step 2: update caption on channel post
    caption = (
        f"{card_name}\nPrice: {price_str}\n❌ SOLD OUT"
        if new_remaining <= 0 else
        f"{card_name}\nPrice: {price_str}\nAvailable: {new_remaining}"
    )
    try:
        await message.bot.edit_message_caption(chat_id=CHANNEL_ID, message_id=post_mid, caption=caption)
    except Exception:
        pass

    # Step 3: adjust any non-shipped order for that user (A + C integration)
    order_cancelled = False
    updated_invoice = None

    with get_db() as conn:
        cur = conn.cursor()
        # pick latest active order
        cur.execute("""
            SELECT id, invoice_no, cards_total, delivery_fee, total, status
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

            # reduce/remove matching order item
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
                oi_price = float(oi["price"])

                # we remove min(qty claimed, item qty). Usually identical.
                remove_qty = min(qty, oi_qty)
                new_qty = oi_qty - remove_qty

                if new_qty <= 0:
                    cur.execute("DELETE FROM order_items WHERE id = ?", (oi_id,))
                else:
                    cur.execute("UPDATE order_items SET qty = ? WHERE id = ?", (new_qty, oi_id))

                # recompute totals from remaining order_items
                cur.execute("SELECT COALESCE(SUM(price * qty), 0) AS cards_total FROM order_items WHERE order_id = ?", (order_id,))
                cards_total = float(cur.fetchone()["cards_total"] or 0)
                delivery_fee = float(ord_row["delivery_fee"] or 0)
                total = cards_total + delivery_fee

                if cards_total <= 0:
                    # C integration: auto-cancel order if empty
                    cur.execute("UPDATE orders SET status = 'cancelled', cards_total = 0, total = 0 WHERE id = ?", (order_id,))
                    order_cancelled = True
                else:
                    cur.execute("UPDATE orders SET cards_total = ?, total = ? WHERE id = ?", (cards_total, total, order_id))

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
    """Accept '1', '1 3 4', '1,3,4'. Return sorted unique ints."""
    parts = text.replace(",", " ").split()
    nums = []
    for p in parts:
        if p.isdigit():
            nums.append(int(p))
    return sorted(set(nums))


async def process_cancel_claims_text(message: Message) -> bool:
    """Returns True if this message was handled by cancel-claims wizard."""
    sess = get_admin_session(ADMIN_ID)
    if not sess:
        return False

    stype = sess.get("session_type")
    text = (message.text or "").strip()

    # Step 1: select user
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

        await _send_user_claimed_cards(message, user_id=int(row["user_id"]), username=row.get("username") or None)
        return True

    # Step 2: select item(s)
    if stype == "cc_select_items":
        if text == "0":
            # go back to user list
            await list_cancel_claim_users(message)
            return True

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

        # validate selection
        chosen = [groups[i-1] for i in nums if 1 <= i <= len(groups)]
        if not chosen:
            await message.answer("❌ Invalid selection.")
            return True

        removed_lines = []
        order_cancelled_any = False
        invoice_touched = None

        for g in chosen:
            res = await _admin_cancel_claim_group(message, user_id=user_id, post_mid=int(g["post_mid"]))
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

        summary = [
            "✅ <b>Claims removed</b>",
            *removed_lines,
        ]

        if invoice_touched:
            summary.append(f"\n🧾 <b>Invoice updated:</b> <code>{invoice_touched}</code>")
        if order_cancelled_any:
            summary.append("⚠️ <b>Order auto-cancelled</b> (no items left).")

        await message.answer("\n".join(summary), parse_mode="HTML")

        # stay in step 2 (show updated list)
        await _send_user_claimed_cards(message, user_id=user_id, username=None)
        return True

    return False


# ===========================
# PENDING PAYMENTS LIST
# ===========================

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
        total = r["total"]

        kb = InlineKeyboardBuilder()

        kb.button(
            text="✅ Approve",
            callback_data=PaymentReviewCB(action="approve", invoice=inv).pack()
        )

        kb.button(
            text="❌ Reject",
            callback_data=PaymentReviewCB(action="reject", invoice=inv).pack()
        )

        kb.adjust(2)

        text = (
            f"<b>Invoice:</b> <code>{inv}</code>\n"
            f"<b>Buyer:</b> @{user}\n"
            f"<b>Total:</b> ${total:.2f}\n"
            f"<b>Status:</b> PAYMENT RECEIVED"
        )

        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ===========================
# LIST READY TO SHIP
# ===========================

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, Command("toship"))
async def list_orders_ready(message: Message):

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT invoice_no, username, total
            FROM orders
            WHERE status = 'ready_to_ship'
            ORDER BY created_at ASC
        """)
        rows = cur.fetchall()

    if not rows:
        await message.answer("📦 No orders currently waiting to be shipped.")
        return

    await message.answer("📦 <b>Orders Ready To Ship:</b>", parse_mode="HTML")

    for r in rows:
        inv = r["invoice_no"]
        user = r["username"] or "NoUsername"
        total = r["total"]

        kb = InlineKeyboardBuilder()

        kb.button(
            text="🚚 Start Shipping",
            callback_data=ShippingActionCB(action="start", invoice=inv).pack()
        )

        kb.button(
            text="❌ Cancel Order",
            callback_data=ShippingActionCB(action="cancel", invoice=inv).pack()
        )

        kb.adjust(2)

        text = (
            f"<b>Invoice:</b> <code>{inv}</code>\n"
            f"<b>Buyer:</b> @{user}\n"
            f"<b>Total:</b> ${total:.2f}\n"
            f"<b>Status:</b> READY TO SHIP"
        )

        await message.answer(text, parse_mode="HTML", reply_markup=kb.as_markup())


# ===========================
# PACKING LIST
# ===========================

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, Command("packlist"))
async def generate_packlist(message: Message):

    with get_db() as conn:
        cur = conn.cursor()

        # Get all orders ready to ship
        cur.execute("""
            SELECT id, invoice_no, username
            FROM orders
            WHERE status = 'ready_to_ship'
            ORDER BY created_at ASC
        """)
        orders = cur.fetchall()

        if not orders:
            await message.answer("📦 No orders currently ready to ship.")
            return

        text = "📦 <b>Packing Checklist</b>\n\n"

        for order in orders:
            order_id = order["id"]
            invoice_no = order["invoice_no"]
            username = order["username"] or "Unknown"

            text += f"<b>{invoice_no}</b> – @{username}\n"

            # Get items linked to this order using order_id
            cur.execute("""
                SELECT card_name, qty
                FROM order_items
                WHERE order_id = ?
            """, (order_id,))

            items = cur.fetchall()

            for item in items:
                text += f"- {item['card_name']} (x{item['qty']})\n"

            text += "\n"

    await message.answer(text, parse_mode="HTML")


# ===========================
# START SHIPPING SESSION
# ===========================

@router.callback_query(ShippingActionCB.filter(F.action == "start"))
async def start_shipping_button(cb: CallbackQuery, callback_data: ShippingActionCB):

    invoice_no = callback_data.invoice

    set_admin_session(ADMIN_ID, "awaiting_tracking", invoice_no)

    await cb.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📦 Shipping started for <code>{invoice_no}</code>\n\nSend tracking number now.",
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

    text = await extract_text_from_photo(message.bot, message)

    # OCR disabled/unavailable (Render) OR OCR failed to read anything
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


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.text, ~F.text.startswith("/"))
async def admin_tracking_catcher(message: Message):

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

    m = TRACKING_REGEX.search(message.text.upper())
    if not m:
        await message.answer("❌ Invalid tracking format.")
        return True

    tracking = m.group(0)

    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
            UPDATE orders
            SET tracking_number = ?, status = 'shipped'
            WHERE invoice_no = ?
        """, (tracking, invoice_no))

        cur.execute("""
            SELECT user_id FROM orders WHERE invoice_no = ?
        """, (invoice_no,))
        row = cur.fetchone()

    clear_admin_session(ADMIN_ID)

    await message.answer(f"✅ Tracking saved for <code>{invoice_no}</code>", parse_mode="HTML")

    if row:
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
