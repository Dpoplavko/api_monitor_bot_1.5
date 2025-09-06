# src/bot.py
# Головний файл, точка входу в застосунок

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import init_db
from handlers import router
from scheduler import setup_scheduler

# Налаштування логування
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

async def main():
    """
    Головна функція для ініціалізації та запуску бота.
    """
    # Ініціалізація бази даних
    await init_db()
    logger.info("Базу даних ініціалізовано.")

    # Ініціалізація бота та диспетчера
    storage = MemoryStorage()
    bot = Bot(token=settings.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=storage)

    # Підключення роутера з обробниками команд
    dp.include_router(router)

    # Ініціалізація планувальника завдань
    scheduler = AsyncIOScheduler(timezone=settings.TZ, job_defaults={
        "max_instances": 1,
        "misfire_grace_time": 30
    })
    
    # Передача залежностей (bot, scheduler) у диспетчер
    dp["bot"] = bot
    dp["scheduler"] = scheduler
    
    # Передаємо конфігурацію в об'єкт bot для доступу в інших модулях
    bot.config = settings

    # Налаштування та запуск завдань моніторингу
    await setup_scheduler(scheduler, bot)
    scheduler.start()
    logger.info("Планувальник завдань запущено.")

    # Видалення вебхука
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот починає роботу в режимі polling...")

    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Роботу бота зупинено.")