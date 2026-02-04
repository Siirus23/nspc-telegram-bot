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

from config import ADMIN_ID

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


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.text)
async def admin_tracking_catcher(message: Message):

    handled = await process_tracking_text(message)
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
