# src/bot.py
# Головний файл, точка входу в застосунок

import asyncio
import logging
import threading

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import init_db, get_all_subscribed_chats, was_version_announced, mark_version_announced
from version import VERSION, RELEASE_NOTES
from handlers import router
from scheduler import setup_scheduler
from prometheus_client import start_http_server
from metrics import BOT_UP
from sysmon import install_log_capture, set_bot_start

# Prometheus metrics are defined in metrics.py

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

    # Включаємо збір помилок у пам'яті та фіксуємо час старту бота
    try:
        install_log_capture()
        set_bot_start()
    except Exception:
        pass

    # Metrics HTTP server (health/metrics)
    def _start_metrics():
        try:
            start_http_server(int(settings.METRICS_PORT))
            BOT_UP.set(1)
        except Exception as e:
            logger.warning(f"Не вдалося запустити metrics сервер: {e}")
    threading.Thread(target=_start_metrics, daemon=True).start()

    # Налаштування та запуск завдань моніторингу
    await setup_scheduler(scheduler, bot)
    scheduler.start()
    logger.info("Планувальник завдань запущено.")

    # Видалення вебхука
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот починає роботу в режимі polling...")

    # Розсилка реліз-нотаток для нової версії всім підписаним чатам (один раз на версію)
    try:
        chats = await get_all_subscribed_chats()
        for chat_id in chats:
            try:
                if not await was_version_announced(int(chat_id), VERSION):
                    await bot.send_message(int(chat_id), RELEASE_NOTES)
                    await mark_version_announced(int(chat_id), VERSION)
            except Exception:
                pass
        logger.info(f"Реліз-нотатки {VERSION} розіслані")
    except Exception as e:
        logger.warning(f"Не вдалося розіслати реліз-нотатки: {e}")

    # Запуск бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Роботу бота зупинено.")