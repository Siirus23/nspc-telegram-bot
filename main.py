import asyncio
from aiogram import Bot, Dispatcher

from config import BOT_TOKEN
from db import init_db

from handlers import admin, claims
import checkout
import shipping_admin
import text_dispatcher
import buyer_panel

 
async def main():
    print("🔹 Initializing database...")
    init_db()

    bot = Bot(token=BOT_TOKEN)
    print("BOT USERNAME:", (await bot.get_me()).username)

    dp = Dispatcher()

    dp.include_router(admin.router)          # CSV + photos
    dp.include_router(shipping_admin.router) # admin panel + shipping
    dp.include_router(claims.router)
    dp.include_router(checkout.router)
    dp.include_router(buyer_panel.router)

    dp.include_router(text_dispatcher.router)

    print("🔹 Bot is ready. Listening for events...")

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        print("🔹 Bot session closed.")


if __name__ == "__main__":
    asyncio.run(main())
