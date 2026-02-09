from aiogram import Router, F
from aiogram.types import Message

import asyncio
import csv
import io

from db import (
    clear_card_listings,
    insert_card_listing,
    start_csv_photo_session,
    get_csv_photo_session,
    get_next_unposted_card,
    mark_card_posted,
    count_unposted_cards,
    clear_session,
)

from config import ADMIN_ID, CHANNEL_ID

router = Router()

print("ULTRA MINIMAL ADMIN ROUTER LOADED")


# ==========================================================
# CSV UPLOAD HANDLER – ADMIN ONLY
# ==========================================================

@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.document)
async def handle_csv_upload(message: Message):

    file_name = message.document.file_name or ""

    if not file_name.lower().endswith(".csv"):
        return

    await message.answer("📥 CSV received. Processing...")

    file = await message.bot.get_file(message.document.file_id)
    file_bytes = await message.bot.download_file(file.file_path)

    try:
        text_data = file_bytes.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text_data))

        rows = list(reader)

        if not rows:
            await message.answer("❌ CSV appears to be empty.")
            return

        required = {"name", "price", "availability"}

        if not required.issubset(set(reader.fieldnames)):
            await message.answer(
                "❌ CSV must contain these headers:\nname, price, availability"
            )
            return

        # ✅ Start Supabase admin photo session
        await start_csv_photo_session(ADMIN_ID)

        # ✅ Clear previous listings
        await clear_card_listings()

        # ✅ Insert CSV rows into Supabase
        for row in rows:
            name = row["name"].strip()
            price = row["price"].strip()
            qty = int(row["availability"])

            await insert_card_listing(
                card_name=name,
                price=price,
                qty=qty,
            )

        await message.answer(
            "✅ CSV processed successfully.\n\n"
            "📸 Now upload the photo(s) for the FIRST card."
        )

    except Exception as e:
        print("CSV PROCESS ERROR:", e)
        await message.answer(f"❌ Error processing CSV: {str(e)}")


        


# ==========================================================
# PHOTO UPLOAD SYSTEM AFTER CSV
# ==========================================================

photo_buffer = {}


@router.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.photo)
async def collect_card_photos(message: Message):

    sess = await get_csv_photo_session(ADMIN_ID)
    if not sess:
        return


    uid = message.from_user.id

    if uid not in photo_buffer:
        photo_buffer[uid] = []
        asyncio.create_task(process_after_delay(uid))

    photo_buffer[uid].append(message)


async def process_after_delay(uid):

    await asyncio.sleep(4)

    messages = photo_buffer.get(uid, [])

    if not messages:
        return

    del photo_buffer[uid]

    await process_card_upload(messages)


async def process_card_upload(messages):

    sess = get_admin_session(ADMIN_ID)

    if not sess or sess.get("session_type") != "awaiting_card_photos":
        return

async def process_card_upload(messages):

    sess = await get_csv_photo_session(ADMIN_ID)
    if not sess:
        return

    card = await get_next_unposted_card()
    if not card:
        await clear_session(ADMIN_ID)
        return

    card_id = card["id"]
    name = card["card_name"]
    price = card["price"]
    qty = card["remaining_qty"]

    caption = f"{name}\nPrice: {price}\nAvailable: {qty}"

    try:
        if len(messages) == 1:
            sent = await messages[0].bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=messages[0].photo[-1].file_id,
                caption=caption
            )
        else:
            from aiogram.types import InputMediaPhoto

            media = [
                InputMediaPhoto(media=m.photo[-1].file_id)
                for m in messages
            ]
            media[0].caption = caption

            sent_msgs = await messages[0].bot.send_media_group(
                chat_id=CHANNEL_ID,
                media=media
            )
            sent = sent_msgs[0]

    except Exception as e:
        print("CHANNEL POST ERROR:", e)
        return

    # ✅ Update Supabase with channel message info
    await mark_card_posted(
        card_id=card_id,
        channel_chat_id=CHANNEL_ID,
        channel_message_id=sent.message_id,
    )

    remaining = await count_unposted_cards()

    if remaining > 0:
        await messages[0].answer(
            f"✅ Posted: {name}\n\n"
            f"📸 Upload photo for NEXT card ({remaining} remaining)."
        )
    else:
        await messages[0].answer("🎉 All cards posted to channel successfully!")
        await clear_session(ADMIN_ID)

        post_sale_message = (
            "📮 <b>NightShade Poké Claims – Post Sale Procedure</b> 📮\n\n"
            "<b>How It Works:</b>\n"
            "🚀 Type <b>/start</b> on @NSPCbot\n"
            "👉 Follow the bot instructions step by step\n\n"
            "<b>Questions?</b>\n"
            "💌 DM Admin at @ILoveCatFoochie\n\n"
            "Thank you again for being part of today’s claim sale! ❤️\n"
            "Looking forward to seeing you again at the next one! 🙌"
        )

        try:
            await messages[0].bot.send_message(
                chat_id=CHANNEL_ID,
                text=post_sale_message,
                parse_mode="HTML"
            )
        except Exception as e:
            print("Failed to send post-sale message:", e)
