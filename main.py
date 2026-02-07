import asyncio

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeChat

from config import BOT_TOKEN, ADMIN_ID
from db import init_db

import admin
import buyer_panel
import checkout
import claims
import shipping_admin
import text_dispatcher


def _merge_commands(*lists: list[BotCommand]) -> list[BotCommand]:
    """Deduplicate by command name (last one wins)."""
    merged: dict[str, BotCommand] = {}
    for lst in lists:
        for cmd in lst:
            merged[cmd.command] = cmd
    return list(merged.values())


async def setup_bot_commands(bot: Bot):
    # Commands shown to ALL users in private chats
    buyer_cmds = _merge_commands(
        [
            BotCommand(command="start", description="🛒Press To Begin Checkout🛒"),
            BotCommand(command="buyerpanel", description="📮Pokedex📮"),
        ]
    )
    await bot.set_my_commands(buyer_cmds, scope=BotCommandScopeAllPrivateChats())

    # Commands shown ONLY in the admin's private chat with the bot
    admin_cmds = _merge_commands(
        buyer_cmds,
        [
            BotCommand(command="adminpanel", description="🛠️Admin panel"),
            BotCommand(command="pending", description="⏳Pending payments"),
            BotCommand(command="toship", description="📦Orders ready to pack"),
            BotCommand(command="packlist", description="📋Packing List"),
            BotCommand(command="approve", description="✅Approve Payment❌"),
        ],
    )
    await bot.set_my_commands(admin_cmds, scope=BotCommandScopeChat(chat_id=ADMIN_ID))


async def main():
    print("ð¹ Initializing database...")
    init_db()

    bot = Bot(token=BOT_TOKEN)
    print("BOT USERNAME:", (await bot.get_me()).username)

    # Register Telegram menu commands (buyers vs admin)
    await setup_bot_commands(bot)

    dp = Dispatcher()

    # Routers
    dp.include_router(admin.router)           # CSV + photos
    dp.include_router(shipping_admin.router)  # admin panel + shipping
    dp.include_router(claims.router)          # claim/cancel in channel threads
    dp.include_router(checkout.router)        # checkout / payment proof / address flow
    dp.include_router(buyer_panel.router)     # buyer panel buttons

    dp.include_router(text_dispatcher.router) # generic private chat text dispatcher

    print("ð¹ Bot is ready. Listening for events...")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        print("ð¹ Bot session closed.")


if __name__ == "__main__":
    asyncio.run(main())
