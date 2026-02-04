from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import get_db, set_admin_session, get_admin_session, clear_admin_session

router = Router()


# ===========================
# BUYER PANEL MENU
# ===========================

def build_buyer_panel():
    kb = InlineKeyboardBuilder()

    kb.button(text="📦 View My Orders", callback_data="buyer:orders")
    kb.button(text="🧾 Resend Invoice", callback_data="buyer:invoice")
    kb.button(text="✏️ Edit Shipping Address", callback_data="buyer:editaddr")
    kb.button(text="📄 My Claims", callback_data="buyer:claims")

    kb.adjust(1)
    return kb.as_markup()


@router.message(F.chat.type == "private", Command("buyerpanel"))
async def show_buyer_panel(message: Message):

    await message.answer(
        "👤 <b>Buyer Control Panel</b>\n\nChoose an action:",
        parse_mode="HTML",
        reply_markup=build_buyer_panel()
    )


# ==========================
# View My Orders
# ===========================

@router.callback_query(F.data == "buyer:orders")
async def view_my_orders(cb: CallbackQuery):

    user_id = cb.from_user.id

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT invoice_no, status, tracking_number
            FROM orders
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,))
        rows = cur.fetchall()

    if not rows:
        await cb.message.answer("📭 You have no orders yet.")
        await cb.answer()
        return

    status_map = {
        "awaiting_payment": "Awaiting Payment",
        "payment_received": "Pending Approval",
        "ready_to_ship": "Ready to Ship",
        "shipped": "Shipped",
        "cancelled": "Cancelled"
    }

    text = ["📦 <b>Your Orders</b>\n"]

    for r in rows:
        status = status_map.get(r["status"], r["status"])
        line = f"• <code>{r['invoice_no']}</code> – {status}"

        if r["tracking_number"]:
            line += f" – {r['tracking_number']}"

        text.append(line)

    await cb.message.answer("\n".join(text), parse_mode="HTML")
    await cb.answer()


# ==========================
# Resend Invoice (Session Based)
# ===========================

@router.callback_query(F.data == "buyer:invoice")
async def resend_invoice_prompt(cb: CallbackQuery):

    set_admin_session(cb.from_user.id, "awaiting_invoice_resend", None)

    await cb.message.answer(
        "Send the invoice number you want to receive again.\n\nExample: INV-00016"
    )

    await cb.answer()


@router.message(F.text)
async def resend_invoice(message: Message):

    sess = get_admin_session(message.from_user.id)

    if not sess or sess.get("session_type") != "awaiting_invoice_resend":
        return

    invoice_no = message.text.strip()

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT *
            FROM orders
            WHERE invoice_no = ?
        """, (invoice_no,))
        order = cur.fetchone()

    if not order:
        await message.answer("❌ Invoice not found.")
        return

    if order["user_id"] != message.from_user.id:
        await message.answer("❌ That invoice does not belong to you.")
        return

    await message.answer("📨 Resending your invoice…")

    try:
        from invoice_pdf import build_invoice_pdf

        pdf = build_invoice_pdf(
            invoice_no=order["invoice_no"],
            delivery_method=order["delivery_method"],
            cards_total_sgd=order["cards_total"],
            delivery_fee_sgd=order["delivery_fee"],
            total_sgd=order["total"],
            paynow_number="93385994",
            paynow_name="Naufal",
            buyer_username=order["username"],
            buyer_address=order["shipping_address"] if "shipping_address" in order.keys() else "",
            items=[]
        )

        await message.bot.send_document(
            chat_id=message.from_user.id,
            document=BufferedInputFile(pdf, filename=f"{invoice_no}.pdf"),
            caption=f"🧾 Resent Invoice: {invoice_no}"
        )

    except Exception as e:
        print("Resend invoice error:", e)
        await message.answer("❌ Failed to resend invoice. Please contact admin.")

    clear_admin_session(message.from_user.id)


# ==========================
# Edit Shipping Address
# ===========================

@router.callback_query(F.data == "buyer:editaddr")
async def edit_address_start(cb: CallbackQuery):

    await cb.message.answer(
        "✏️ <b>Update Shipping Address</b>\n\n"
        "To update your address, please contact the admin directly with your invoice number:\n\n"
        "👉 @ILoveCatFoochie\n\n"
        "Include in your message:\n"
        "• Invoice number (e.g. INV-00016)\n"
        "• Your new full address\n\n"
        "This ensures faster and more accurate updates.",
        parse_mode="HTML"
    )

    await cb.answer()



# ==========================
# View My Claims
# ===========================

@router.callback_query(F.data == "buyer:claims")
async def show_my_claims(cb: CallbackQuery):

    user_id = cb.from_user.id

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT cl.card_name
            FROM claims c
            JOIN card_listing cl
              ON c.channel_chat_id = cl.channel_chat_id
             AND c.channel_message_id = cl.channel_message_id
            WHERE c.user_id = ?
              AND c.status = 'active'
        """, (user_id,))
        rows = cur.fetchall()

    if not rows:
        await cb.message.answer("You have no active claims.")
        await cb.answer()
        return

    text = ["📄 <b>Your Active Claims</b>\n"]

    for r in rows:
        text.append(f"• {r['card_name']}")

    await cb.message.answer("\n".join(text), parse_mode="HTML")
    await cb.answer()
