# src/scheduler.py
# Логіка моніторингу: перевірка API та відправка сповіщень

import asyncio
import datetime
import httpx
import logging
import time
import json

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import settings
from database import (MonitoredAPI, update_api_status, get_api_by_id, 
                      log_check_to_history, create_incident, end_incident, get_all_active_apis, get_stats_for_period)
from utils import format_api_status, format_timedelta

logger = logging.getLogger(__name__)

async def check_api(bot: Bot, api_id: int):
    """
    Основна функція перевірки одного API з логікою порогів та інцидентів.
    """
    api = await get_api_by_id(api_id)
    if not api or not api.is_active:
        return

    start_time = time.monotonic()
    is_currently_ok = False
    response_time_ms = -1
    status_code = None
    error_str = None
    
    try:
        async with httpx.AsyncClient(timeout=api.timeout, headers=api.headers) as client:
            request_params = {"method": api.method.upper(), "url": api.url}
            if api.request_body:
                request_params["json"] = api.request_body

            response = await client.send(client.build_request(**request_params))
            response_time_ms = int((time.monotonic() - start_time) * 1000)
            status_code = response.status_code
            
            if status_code != api.expected_status:
                raise ValueError(f"Очікувався статус {api.expected_status}, отримано {status_code}")

            if api.json_keys:
                json_data = response.json()
                missing_keys = [k.strip() for k in api.json_keys.split(',') if k.strip() not in json_data]
                if missing_keys:
                    raise ValueError(f"У JSON відсутні ключі: {', '.join(missing_keys)}")

            is_currently_ok = True

    except Exception as e:
        response_time_ms = int((time.monotonic() - start_time) * 1000)
        error_str = str(e)
        logger.warning(f"ПЕРЕВІРКА НЕВДАЛА: API '{api.name}' (ID: {api.id}): {error_str}")

    await log_check_to_history(api_id, is_currently_ok, response_time_ms, status_code)
    
    update_data = {
        "last_checked": datetime.datetime.utcnow(),
        "last_response_time": response_time_ms,
        "last_status_code": status_code,
        "last_error": error_str
    }
    
    if is_currently_ok:
        update_data["consecutive_failures"] = 0
        update_data["consecutive_successes"] = api.consecutive_successes + 1
        
        if not api.is_up and update_data["consecutive_successes"] >= settings.RECOVERY_THRESHOLD:
            logger.info(f"ВІДНОВЛЕННЯ: API '{api.name}' (ID: {api.id}) доступне {settings.RECOVERY_THRESHOLD} р.")
            update_data["is_up"] = True
            
            incident_end_time = datetime.datetime.utcnow()
            await end_incident(api.id, api.incident_start_time, incident_end_time)
            
            downtime = incident_end_time - api.incident_start_time
            requests_during_downtime = api.consecutive_failures + update_data["consecutive_successes"]
            
            recovery_message = (
                f"✅ <b>ВІДНОВЛЕННЯ: {api.name}</b>\n\n"
                f"Сервіс стабільно працює.\n\n"
                f"<b>Деталі інциденту:</b>\n"
                f"  - Початок: <code>{api.incident_start_time.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
                f"  - Кінець: <code>{incident_end_time.strftime('%Y-%m-%d %H:%M:%S')}</code>\n"
                f"  - Тривалість: <b>{format_timedelta(downtime)}</b>\n"
                f"  - Невдалих перевірок: {api.consecutive_failures}"
            )
            await bot.send_message(bot.config.ADMIN_USER_ID, recovery_message)
            update_data["incident_start_time"] = None
        
    else: # Невдала перевірка
        update_data["consecutive_successes"] = 0
        update_data["consecutive_failures"] = api.consecutive_failures + 1
        
        if api.is_up and update_data["consecutive_failures"] >= settings.FAILURE_THRESHOLD:
            logger.error(f"ПАДІННЯ: API '{api.name}' (ID: {api.id}) не відповідає {settings.FAILURE_THRESHOLD} р.")
            update_data["is_up"] = False
            # Час початку інциденту - це час першої невдалої перевірки
            update_data["incident_start_time"] = datetime.datetime.utcnow() - datetime.timedelta(seconds=api.check_interval * (update_data["consecutive_failures"] - 1))
            await create_incident(api.id, update_data["incident_start_time"])
            
            await bot.send_message(
                chat_id=bot.config.ADMIN_USER_ID,
                text=f"🔴 <b>ПАДІННЯ: {api.name}</b>\n\n{format_api_status(api, update_data)}"
            )

    await update_api_status(api.id, update_data)

async def send_daily_summary(bot: Bot):
    """Надсилає щоденний звіт по всім активним моніторам."""
    logger.info("Починаю формування щоденного звіту...")
    active_apis = await get_all_active_apis()
    if not active_apis:
        logger.info("Немає активних моніторів для щоденного звіту.")
        return

    summary_header = f"☀️ <b>Щоденний звіт за {datetime.date.today().strftime('%d-%m-%Y')}</b>\n\n"
    summary_parts = []

    for api in active_apis:
        status_icon = "🟢" if api.is_up else "🔴"
        stats = await get_stats_for_period(api.id, "24h")
        
        part = (
            f"<b>{status_icon} {api.name}</b> (ID: {api.id})\n"
            f"  - Аптайм: {stats.get('uptime_percent', 100):.2f}%\n"
            f"  - Падінь: {stats.get('incident_count', 0)}\n"
            f"  - Час простою: {format_timedelta(stats.get('total_downtime', datetime.timedelta()))}\n"
            f"  - Сер. відповідь: {int(stats.get('avg_response_time_ms') or 0)} мс\n"
        )
        summary_parts.append(part)

    full_summary = summary_header + "\n".join(summary_parts)
    
    try:
        await bot.send_message(bot.config.ADMIN_USER_ID, full_summary)
        logger.info("Щоденний звіт успішно надіслано.")
    except Exception as e:
        logger.error(f"Не вдалося надіслати щоденний звіт: {e}")


async def add_job_to_scheduler(scheduler: AsyncIOScheduler, bot: Bot, api: MonitoredAPI):
    job_id = f"api_check_{api.id}"
    scheduler.add_job(
        check_api, trigger=IntervalTrigger(seconds=api.check_interval),
        args=[bot, api.id], id=job_id, name=f"Check {api.name}", replace_existing=True,
        next_run_time=datetime.datetime.now() + datetime.timedelta(seconds=2)
    )
    logger.info(f"Завдання {job_id} ({api.name}) додано/оновлено з інтервалом {api.check_interval} сек.")

def remove_job_from_scheduler(scheduler: AsyncIOScheduler, api_id: int):
    job_id = f"api_check_{api_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"Завдання {job_id} видалено.")

async def setup_scheduler(scheduler: AsyncIOScheduler, bot: Bot):
    logger.info("Налаштування завдань планувальника...")
    active_apis = await get_all_active_apis()
    for api in active_apis:
        await add_job_to_scheduler(scheduler, bot, api)
    logger.info(f"Додано {len(active_apis)} завдань моніторингу.")
    
    # Додаємо щоденну розсилку
    scheduler.add_job(send_daily_summary, 'cron', hour=9, minute=0, args=[bot], timezone='Europe/Kiev')
    logger.info("Додано завдання для щоденної розсилки о 9:00.")