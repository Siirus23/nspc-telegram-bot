# checkout.py
import re
from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    BufferedInputFile,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_ID, CHANNEL_ID, CHANNEL_USERNAME

from db import (
    get_stale_claims_for_user,
    cancel_all_claims_for_user,
    get_user_claims_summary,
    upsert_checkout,
    get_checkout,
    set_payment_proof,
    get_checkout_by_invoice,
)

from invoice_pdf import build_invoice_pdf
from callbacks import PaymentReviewCB

router = Router()

# =========================
# CONFIG
# =========================
PAYNOW_NUMBER = "93385994"
PAYNOW_NAME = "Naufal"

TRACKED_FEE_SGD = 3.50
SELF_PICKUP_TEXT = "806 Woodlands St 81, in front of Rainbow Mart"

# =========================
# ADDRESS PARSING
# =========================
ADDRESS_FIELDS = [
    "Name",
    "Street Name",
    "Unit Number",
    "Postal Code",
    "Phone Number",
]

def parse_address_block(text: str):
    if not text:
        return None

    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    data = {}

    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()

        for field in ADDRESS_FIELDS:
            if key == field.lower():
                data[field] = value

    if any(not data.get(f) for f in ADDRESS_FIELDS):
        return None

    return {
        "name": data["Name"].strip(),
        "street": data["Street Name"].strip(),
        "unit": data["Unit Number"].strip(),
        "postal": re.sub(r"\s+", "", data["Postal Code"]),
        "phone": re.sub(r"\s+", "", data["Phone Number"]),
    }

def address_template():
    return (
        "————— COPY FROM HERE —————\n"
        "Name :\n"
        "Street Name :\n"
        "Unit Number :\n"
        "Postal Code :\n"
        "Phone Number :\n"
        "————— COPY UNTIL HERE —————"
    )

# =========================
# KEYBOARDS
# =========================
def kb_buyer_home(has_claims: bool):
    kb = InlineKeyboardBuilder()

    if has_claims:
        kb.button(text="🦇 Go To Checkout 🦇", callback_data="buyer:go_delivery")
        kb.button(text="🎒 Open My Bag 🎒", callback_data="buyer:panel")
        kb.adjust(1)
    else:
        kb.button(text="📜 Trainer Guide", callback_data="buyer:howto")
        kb.adjust(1)

    return kb.as_markup()

def kb_delivery():
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Tracked Mail", callback_data="checkout:delivery:tracked")
    kb.button(text="🏠 Self Collection", callback_data="checkout:delivery:self")
    kb.adjust(1)
    return kb.as_markup()

def kb_continue():
    kb = InlineKeyboardBuilder()
    kb.button(text="🧾 Confirm Checkout", callback_data="checkout:continue")
    kb.adjust(1)
    return kb.as_markup()


# =========================
#  HELPERS
# =========================

def parse_price_to_float(price_str: str) -> float:
    """
    Converts price strings like:
    "$12", "SGD 12", "12.50" → 12.0 / 12.5
    """
    if price_str is None:
        return 0.0

    s = str(price_str).upper().strip()
    s = s.replace("SGD", "").replace("$", "").strip()

    try:
        return float(s)
    except ValueError:
        return 0.0


# =========================
# CLAIM SUMMARY
# =========================
def format_claim_summary(items):
    total = 0.0
    lines = ["🎴 <b>Your Claimed Cards</b>\n"]

    for i, it in enumerate(items, start=1):
        card = it["card_name"]
        qty = int(it["qty"])
        price = parse_price_to_float(it["price"])
        total += price * qty

        if qty == 1:
            lines.append(f"{i}. {card}\n   💰 ${price:.2f} SGD")
        else:
            lines.append(f"{i}. {card} (x{qty})\n   💰 ${price:.2f} SGD each")

    lines.append(f"\n<b>Total: ${total:.2f} SGD</b>")
    return "\n".join(lines), total

# =========================
# /start — BUYER HOME
# =========================
@router.message(F.chat.type == "private", Command("start"))
async def dm_start(message: Message):
    user_id = message.from_user.id

    # Auto-cancel stale claims (24h)
    stale = await get_stale_claims_for_user(user_id=user_id, hours=24)
    if stale:
        await cancel_all_claims_for_user(user_id)
        await message.answer(
            "⏰ <b>Your claims expired</b>\n\n"
            "Claims are held for <b>24 hours</b>.\n"
            "They’ve been released. Please claim again.",
            parse_mode="HTML",
        )
        return

    items = await get_user_claims_summary(user_id)
    if not items:
        upsert_checkout(user_id, stage="idle")
        await message.answer(
            "🎴 <b>NightShade Poké Claims</b>\n\n"
            "Reply <b>claim</b> under a card post to reserve it.\n"
            "Then come back here to checkout.",
            parse_mode="HTML",
            reply_markup=kb_buyer_home(False),
        )
        return

    summary, total = format_claim_summary(items)

    upsert_checkout(
        user_id,
        stage="choose_delivery",
        cards_total=total,
        delivery_fee=0,
        total=total,
        invoice_no=None,
        delivery_method=None,
    )

    await message.answer(
        f"🧺 <b>Your Bag</b>\n\n{summary}\n\nChoose delivery:",
        parse_mode="HTML",
        reply_markup=kb_buyer_home(True),
    )

# =========================
# DELIVERY PICK
# =========================
@router.callback_query(F.data == "buyer:go_delivery")
async def buyer_go_delivery(cb: CallbackQuery):
    await cb.message.answer(
        "<b>Choose delivery:</b>\n"
        f"• 📦 Tracked Mail: +${TRACKED_FEE_SGD:.2f}\n"
        "• 🏠 Self Collection: $0",
        parse_mode="HTML",
        reply_markup=kb_delivery(),
    )
    await cb.answer()

@router.callback_query(F.data.startswith("checkout:delivery:"))
async def delivery_pick(cb: CallbackQuery):
    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    if ck.get("stage") != "choose_delivery":
        await cb.answer()
        return

    choice = cb.data.split(":")[2]

    if choice == "tracked":
        fee = TRACKED_FEE_SGD
        method = "tracked"
    elif choice == "self":
        fee = 0.0
        method = "self"
    else:
        await cb.answer()
        return

    cards_total = float(ck.get("cards_total") or 0)
    total = cards_total + fee

    upsert_checkout(
        user_id,
        stage="awaiting_confirm",
        delivery_method=method,
        delivery_fee=fee,
        total=total,
    )

    await cb.message.answer("Ready to generate invoice?", reply_markup=kb_continue())
    await cb.answer()

# =========================
# GENERATE INVOICE
# =========================
@router.callback_query(F.data == "checkout:continue")
async def checkout_continue(cb: CallbackQuery):
    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    if ck.get("stage") != "awaiting_confirm":
        await cb.answer()
        return

    items = await get_user_claims_summary(user_id)
    if not items:
        await cb.message.answer("⚠️ No active claims.")
        await cb.answer()
        return

    summary, cards_total = format_claim_summary(items)
    delivery_fee = float(ck.get("delivery_fee") or 0)
    total = cards_total + delivery_fee

    invoice_no = f"INV-{int(datetime.now(timezone.utc).timestamp())}"

    upsert_checkout(user_id, stage="awaiting_payment", invoice_no=invoice_no)

    invoice_items = [
        {"name": it["card_name"], "qty": it["qty"], "price": it["price"]}
        for it in items
    ]

    pdf = build_invoice_pdf(
        invoice_no=invoice_no,
        delivery_method=ck["delivery_method"],
        cards_total_sgd=cards_total,
        delivery_fee_sgd=delivery_fee,
        total_sgd=total,
        paynow_number=PAYNOW_NUMBER,
        paynow_name=PAYNOW_NAME,
        buyer_username=cb.from_user.username or "",
        buyer_address="Address to be provided after payment",
        items=invoice_items,
    )

    await cb.message.answer_document(
        BufferedInputFile(pdf, filename=f"{invoice_no}.pdf"),
        caption=f"🧾 Invoice <code>{invoice_no}</code>\nTotal: ${total:.2f} SGD\n\nSend payment proof here.",
        parse_mode="HTML",
    )

    await cb.answer()

# =========================
# PAYMENT PROOF
# =========================
@router.message(F.chat.type == "private", (F.photo | F.document))
async def payment_proof_received(message: Message):
    ck = get_checkout(message.from_user.id) or {}
    if ck.get("stage") != "awaiting_payment":
        return

    invoice_no = ck.get("invoice_no")
    if not invoice_no:
        return

    if message.photo:
        set_payment_proof(invoice_no, message.photo[-1].file_id, "photo")
    elif message.document:
        set_payment_proof(invoice_no, message.document.file_id, "document")

    upsert_checkout(message.from_user.id, stage="payment_submitted")

    await message.answer(
        "✅ Payment proof received.\nPlease wait for admin approval."
    )

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Approve", callback_data=PaymentReviewCB(action="approve", invoice=invoice_no).pack())
    kb.button(text="❌ Reject", callback_data=PaymentReviewCB(action="reject", invoice=invoice_no).pack())
    kb.adjust(2)

    await message.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 Payment received\nInvoice: {invoice_no}",
        reply_markup=kb.as_markup(),
    )
    
# =========================
# CAPTURE ADDRESS
# =========================

@router.message(F.chat.type == "private")
async def capture_address(message: Message):
    user_id = message.from_user.id
    ck = get_checkout(user_id) or {}

    if ck.get("stage") != "awaiting_address":
        return  # ignore unrelated messages

    parsed = parse_address_block(message.text)
    if not parsed:
        await message.answer(
            "❌ <b>Address format not recognised</b>\n\n"
            "Please copy the template below, fill it in, and send it in <b>ONE message</b>:\n\n"
            f"<code>{address_template()}</code>",
            parse_mode="HTML"
        )
        return

    # Temporarily store address in checkout session
    upsert_checkout(
        user_id,
        stage="confirm_address",
        temp_address=parsed  # stored as JSON / dict
    )

    preview = (
        "📦 <b>Please confirm your shipping address</b>\n\n"
        f"<b>Name:</b> {parsed['name']}\n"
        f"<b>Street:</b> {parsed['street']}\n"
        f"<b>Unit:</b> {parsed['unit']}\n"
        f"<b>Postal:</b> {parsed['postal']}\n"
        f"<b>Phone:</b> {parsed['phone']}\n\n"
        "Is this correct?"
    )

    await message.answer(
        preview,
        parse_mode="HTML",
        reply_markup=kb_confirm_address()
    )

@router.callback_query(F.data == "checkout:address:confirm")
async def address_confirm(cb: CallbackQuery):
    user_id = cb.from_user.id
    ck = get_checkout(user_id) or {}

    if ck.get("stage") != "confirm_address":
        await cb.answer()
        return

    addr = ck.get("temp_address")
    invoice_no = ck.get("invoice_no")

    if not addr or not invoice_no:
        await cb.message.answer("❌ Address session expired. Please try again.")
        await cb.answer()
        return

   
    # Persist address to DB
    await save_shipping_address(
        invoice_no=invoice_no,
        **addr
    )

     # Move order status to packing (Supabase)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE orders
            SET status = 'packing'
            WHERE invoice_no = $1
            """,
            invoice_no
        )

        
    # Move order forward
    upsert_checkout(
        user_id,
        stage="packing",
        temp_address=None
    )

    await cb.message.answer(
        "✅ <b>Address confirmed!</b>\n\n"
        "Your order is now being prepared 🧺📦\n"
        "You’ll be notified once it’s shipped.",
        parse_mode="HTML"
    )

    # Notify admin
    try:
        await cb.message.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "📦 <b>Order ready to pack</b>\n\n"
                f"Invoice: <code>{invoice_no}</code>\n"
                f"Buyer: @{cb.from_user.username or 'NoUsername'}"
            ),
            parse_mode="HTML"
        )
    except Exception:
        pass



@router.callback_query(F.data == "checkout:address:reenter")
async def address_reenter(cb: CallbackQuery):
    user_id = cb.from_user.id

    upsert_checkout(
        user_id,
        stage="awaiting_address",
        temp_address=None
    )

    await cb.message.answer(
        "✏️ No problem — please send your address again using this template:\n\n"
        f"<code>{address_template()}</code>",
        parse_mode="HTML"
    )
    await cb.answer()
