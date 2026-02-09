# buyer_panel.py
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

from invoice_pdf import build_invoice_pdf

from db import (
    get_orders_by_user,
    get_latest_order_by_user,
    get_active_claims_by_user,
    set_session,
    get_session,
    clear_session
    STATUS_AWAITING_PAYMENT,
    STATUS_VERIFYING,
    STATUS_AWAITING_ADDRESS,
    STATUS_PACKING_PENDING,
    STATUS_PACKED,
    STATUS_SHIPPED,
)



router = Router()

# ===========================
# UI TEXT (NightShade Poké Mart: dark + goofy)
# ===========================
ADMIN_USERNAME = "@ILoveCatFoochie"  # change if needed

def build_buyer_panel():
    kb = InlineKeyboardBuilder()

    kb.button(text="📦 My Orders (Past Hauls)", callback_data="buyer:orders")
    kb.button(text="📦 Latest Order Status", callback_data="buyer:status")
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

    rows = await get_orders_by_user(user_id)

    if not rows:
        await cb.message.answer("📭 No orders found… your bag is suspiciously empty.")
        await cb.answer()
        return

    status_map = {
        "pending_payment": "Awaiting Payment",
        "payment_received": "Pending Approval",
        "verifying": "Payment Verified",
        "ready_to_ship": "Ready to Ship",
        "packed": "Packed",
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
    await set_session(
        user_id=cb.from_user.id,
        role="buyer",
        session_type="resend_invoice",
        data={"step": "awaiting_invoice_no"},
    )

    await cb.message.answer(
        "🧾🕯️ <b>Summon your invoice scroll</b>\n\n"
        "Please send the invoice number.\n"
        "Example: <code>INV-000016</code>",
        parse_mode="HTML",
    )
    await cb.answer()

@router.message(F.chat.type == "private", F.text.regexp(r"^INV-\d+"))
async def resend_invoice(message: Message):
    session = await get_session(message.from_user.id)

    if not session:
        return

    if session["role"] != "buyer" or session["session_type"] != "resend_invoice":
        return

    invoice_no = message.text.strip()

    # Fetch order (Supabase)
    pool = await get_pool()
    async with pool.acquire() as conn:
        order = await conn.fetchrow(
            """
            SELECT *
            FROM orders
            WHERE invoice_no = $1
            """,
            invoice_no,
        )

        if not order:
            await message.answer("❌ Invoice not found.")
            return

        if int(order["user_id"]) != message.from_user.id:
            await message.answer("❌ That invoice does not belong to you.")
            return

        items = await conn.fetch(
            """
            SELECT card_name, price, qty
            FROM order_items
            WHERE order_id = $1
            ORDER BY id ASC
            """,
            order["id"],
        )

        addr = await conn.fetchrow(
            """
            SELECT name, street_name, unit_number, postal_code, phone_number, confirmed
            FROM shipping_address
            WHERE order_id = $1
            """,
            order["id"],
        )

    invoice_items = [
        {"name": r["card_name"], "qty": int(r["qty"]), "price": float(r["price"])}
        for r in items
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

    await clear_session(message.from_user.id)


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

    rows = await get_active_claims_by_user(user_id)

    if not rows:
        await cb.message.answer("🕸️ You have no active claims right now.")
        await cb.answer()
        return

    text = ["🎴 <b>Your Active Claims</b>\n"]

    for r in rows:
        text.append(f"• {r['card_name']} — {r['price']}")

    text.append("\n🕯️ When you're ready to checkout, type <b>/start</b>.")
    await cb.message.answer("\n".join(text), parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data == "buyer:status")
async def show_latest_order_status(cb: CallbackQuery):
    user_id = cb.from_user.id

    order = await get_latest_order_by_user(user_id)

    if not order:
        await cb.message.answer("🛒 You have no active orders.")
        await cb.answer()
        return

    invoice = order["invoice_no"]
    status = order["status"]

    lines = [
        f"🧾 <b>Invoice:</b> <code>{invoice}</code>",
        f"📦 <b>Status:</b> {status.replace('_', ' ').title()}",
    ]

    if status == STATUS_AWAITING_PAYMENT:
        lines.append("\n⏳ Awaiting payment.")

    elif status == STATUS_VERIFYING:
        lines.append("\n🔍 Payment is being verified.")

    elif status == STATUS_AWAITING_ADDRESS:
        lines.append("\n📮 Awaiting shipping address.")

    elif status == STATUS_PACKING_PENDING:
        lines.append("\n📦 Your order is being packed.")

    elif status == STATUS_PACKED:
        lines.append("\n🚚 Your order is ready to ship.")

    elif status == STATUS_SHIPPED:
        tracking = order["tracking_number"]

        lines.append("\n🚚 <b>Your order has been shipped!</b>")
        lines.append(f"<b>Tracking:</b> <code>{tracking}</code>")
        lines.append(
            "<b>Track here:</b>\n"
            f"https://www.singpost.com/track-items?trackNums={tracking}"
        )

        # Send status text
        await cb.message.answer("\n".join(lines), parse_mode="HTML")

        # Send proof photo if exists
        proof = order["shipping_proof_file_id"]
        if proof:
            await cb.message.answer_photo(
                photo=proof,
                caption="📦 Proof of shipping"
            )

        await cb.answer()
        return

    # Non-shipped statuses
    await cb.message.answer("\n".join(lines), parse_mode="HTML")
    await cb.answer()


