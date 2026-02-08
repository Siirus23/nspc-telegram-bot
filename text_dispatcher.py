from aiogram import Router, F
from aiogram.types import Message
# TEMP: SQLite admin session helpers removed during Supabase migration


from config import ADMIN_ID
import checkout

router = Router()


@router.message(F.chat.type == "private", F.text)
async def private_text_dispatch(message: Message):

    
    user_id = message.from_user.id

    # Ignore commands
    if message.text and message.text.startswith("/"):
        return

    # Buyer address flow
    handled = await checkout.process_address_text(message)
    if handled:
        return

    # Default fallback
    await message.answer(
        "🤖 Please use the bot buttons/commands.\n"
        "If you need help, DM admin."
    )
