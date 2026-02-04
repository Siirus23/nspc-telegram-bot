import asyncio
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message
from config import BOT_TOKEN

router = Router()

@router.message()
async def echo(message: Message):
    print("ECHO RECEIVED:", message.text)
    await message.answer("You said: " + str(message.text))

async def main():
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    print("Test bot running...")
    await dp.start_polling(bot)

asyncio.run(main())
