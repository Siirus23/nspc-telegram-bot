# buyer_panel.py
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from db import get_db, set_admin_session, get_admin_session, clear_admin_session
from invoice_pdf import build_invoice_pdf

router = Router()

# ===========================
# UI TEXT (NightShade Poké Mart: dark + goofy)
# ===========================
ADMIN_USERNAME = "@ILoveCatFoochie"  # change if needed

def build_buyer_panel():
    kb = InlineKeyboardBuilder()

    kb.button(text="📦 My Orders (Past Hauls)", callback_data="buyer:orders")
    kb.button(text="🧾 Resend Invoice (Summon Scroll)", callback_data="buyer:invoice")
    kb.button(text="✏️ Edit Shipping Address (Ask Shopkeeper)", callback_data="buyer:editaddr")
    kb.button(text="🎴 My Claims (Current Bag)", callback_data="buyer:claims")

    kb.adjust(1)
    return kb.as_markup()

# ===========================
# /buyerpanel
# ===========================
@router.message(F.chat.type == "private", Command("buyerpanel"))
async def show_buyer_panel(message: Message):
    await message.answer(
        "🌑🎒 <b>NightShade Buyer Panel</b>\n"
        "Welcome back, Trainer… what are we doing today? 🕯️\n\n"
        "Choose an action:",
        parse_mode="HTML",
        reply_markup=build_buyer_panel(),
    )

# ==========================
# View My Orders
# ==========================
@router.callback_query(F.data == "buyer:orders")
async def view_my_orders(cb: CallbackQuery):
    user_id = cb.from_user.id

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT invoice_no, status, tracking_number, created_at
            FROM orders
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        )
        rows = cur.fetchall()

    if not rows:
        await cb.message.answer("📭 No orders found… your bag is suspiciously empty.")
        await cb.answer()
        return

    status_map = {
        "pending_payment": "Awaiting Payment",
        "payment_received": "Pending Approval",
        "verifying": "Payment Verified",
        "ready_to_ship": "Ready to Ship",
        "shipped": "Shipped",
        "rejected": "Rejected",
        "cancelled": "Cancelled",
    }

    text = ["📦 <b>Your Orders</b>\n"]

    for r in rows:
        status = status_map.get(r["status"], str(r["status"]))
        line = f"• <code>{r['invoice_no']}</code> — {status}"

        if r["tracking_number"]:
            line += f" — <code>{r['tracking_number']}</code>"

        text.append(line)

    await cb.message.answer("\n".join(text), parse_mode="HTML")
    await cb.answer()

# ==========================
# Resend Invoice (Session Based)
# ==========================
@router.callback_query(F.data == "buyer:invoice")
async def resend_invoice_prompt(cb: CallbackQuery):
    # Reuse your existing session store (even though the name says "admin_session")
    set_admin_session(cb.from_user.id, "awaiting_invoice_resend", None)

    await cb.message.answer(
        "🧾🕯️ <b>Summon your invoice scroll</b>\n\n"
        "Send the invoice number you want again.\n"
        "Example: <code>INV-000016</code>\n\n"
        "Tip: Use <b>My Orders</b> to copy the invoice number.",
        parse_mode="HTML",
    )
    await cb.answer()

@router.message(F.chat.type == "private", F.text.regexp(r"^INV-\d+"))
async def resend_invoice(message: Message):
    sess = get_admin_session(message.from_user.id)
    if not sess or sess.get("session_type") != "awaiting_invoice_resend":
        return

    invoice_no = (message.text or "").strip()

    # Load order + items + address (if any)
    with get_db() as conn:
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM orders
            WHERE invoice_no = ?
            """,
            (invoice_no,),
        )
        order = cur.fetchone()

        if not order:
            await message.answer("❌ Invoice not found.")
            return

        if int(order["user_id"]) != message.from_user.id:
            await message.answer("❌ That invoice does not belong to you.")
            return

        # Order items
        cur.execute(
            """
            SELECT card_name, price, qty
            FROM order_items
            WHERE order_id = ?
            ORDER BY id ASC
            """,
            (order["id"],),
        )
        item_rows = cur.fetchall()

        # Shipping address (tracked only, if exists)
        cur.execute(
            """
            SELECT name, street_name, unit_number, postal_code, phone_number, confirmed
            FROM shipping_address
            WHERE order_id = ?
            """,
            (order["id"],),
        )
        addr = cur.fetchone()

    invoice_items = [
        {"name": r["card_name"], "qty": int(r["qty"]), "price": float(r["price"])}
        for r in item_rows
    ]

    buyer_address = ""
    if addr:
        buyer_address = (
            f"Name: {addr['name']}\n"
            f"Street Name: {addr['street_name']}\n"
            f"Unit Number: {addr['unit_number']}\n"
            f"Postal Code: {addr['postal_code']}\n"
            f"Phone Number: {addr['phone_number']}\n"
            f"Confirmed: {'YES' if int(addr['confirmed'] or 0) == 1 else 'NO'}"
        )

    await message.answer("🕯️ Summoning your invoice scroll…")

    try:
        pdf = build_invoice_pdf(
            invoice_no=order["invoice_no"],
            delivery_method=order["delivery_method"],
            cards_total_sgd=float(order["cards_total"] or 0),
            delivery_fee_sgd=float(order["delivery_fee"] or 0),
            total_sgd=float(order["total"] or 0),
            paynow_number="93385994",
            paynow_name="Naufal",
            buyer_username=order["username"] or "",
            buyer_address=buyer_address,
            items=invoice_items,
        )

        await message.bot.send_document(
            chat_id=message.from_user.id,
            document=BufferedInputFile(pdf, filename=f"{invoice_no}.pdf"),
            caption=f"🧾🌑 Resent Invoice: <code>{invoice_no}</code>",
            parse_mode="HTML",
        )

    except Exception as e:
        print("Resend invoice error:", e)
        await message.answer("❌ Failed to resend invoice. Please contact admin.")

    clear_admin_session(message.from_user.id)

# ==========================
# Edit Shipping Address
# ==========================
@router.callback_query(F.data == "buyer:editaddr")
async def edit_address_start(cb: CallbackQuery):
    await cb.message.answer(
        "✏️🌑 <b>Update Shipping Address</b>\n\n"
        "For safety + accuracy, address edits are handled by the shopkeeper.\n\n"
        f"👉 Contact: {ADMIN_USERNAME}\n\n"
        "Include in your message:\n"
        "• Invoice number (e.g. <code>INV-000016</code>)\n"
        "• Your new full address\n\n"
        "🕯️ Tip: If your order is already shipped, address changes may not be possible.",
        parse_mode="HTML",
    )
    await cb.answer()

# ==========================
# View My Claims
# ==========================
@router.callback_query(F.data == "buyer:claims")
async def show_my_claims(cb: CallbackQuery):
    user_id = cb.from_user.id

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cl.card_name, cl.price, cl.channel_message_id
            FROM claims c
            JOIN card_listing cl
              ON c.channel_chat_id = cl.channel_chat_id
             AND c.channel_message_id = cl.channel_message_id
            WHERE c.user_id = ?
              AND c.status = 'active'
            ORDER BY c.claim_order ASC
            """,
            (user_id,),
        )
        rows = cur.fetchall()

    if not rows:
        await cb.message.answer("🕸️ You have no active claims right now.")
        await cb.answer()
        return

    text = ["🎴 <b>Your Active Claims</b>\n"]

    for r in rows:
        # Price is stored as string sometimes; show raw
        price = r["price"]
        text.append(f"• {r['card_name']} — {price}")

    text.append("\n🕯️ When you're ready to checkout, type <b>/start</b>.")
    await cb.message.answer("\n".join(text), parse_mode="HTML")
    await cb.answer()