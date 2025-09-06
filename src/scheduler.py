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
                      log_check_to_history, create_incident, end_incident, get_all_active_apis, get_stats_for_period,
                      get_recent_history_points, save_ml_metric, get_latest_ml_metric, log_anomaly_event, get_last_anomaly_time,
                      get_subscribers_for_api, get_or_create_notification_state, update_down_reminder_time)
from utils import format_api_status, format_timedelta, robust_stats, detect_anomaly

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

            # simple retry/backoff
            max_retries = int(getattr(bot.config, 'REQUEST_RETRIES', 1) or 1)
            backoff_base = float(getattr(bot.config, 'REQUEST_BACKOFF', 0.5) or 0.5)
            for attempt in range(max_retries):
                try:
                    response = await client.send(client.build_request(**request_params))
                    break
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    if attempt < max_retries - 1:
                        await asyncio.sleep(backoff_base * (2 ** attempt))
                    else:
                        raise e

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

    # Раннє попередження (ML-аномалія) — тільки якщо ML увімкнено і є успішна відповідь
    if settings.ML_ENABLED and response_time_ms >= 0 and is_currently_ok:
        try:
            latest_metrics = await get_latest_ml_metric(api_id)
            if latest_metrics and latest_metrics.ucl_ms:
                is_anom, score = detect_anomaly(response_time_ms, latest_metrics.ucl_ms)
                if is_anom:
                    # Перевіряємо cooldown, щоб не спамити
                    last_ts = await get_last_anomaly_time(api_id)
                    too_soon = False
                    if last_ts:
                        delta = datetime.datetime.utcnow() - last_ts
                        too_soon = delta.total_seconds() < settings.ANOMALY_COOLDOWN_MINUTES * 60
                    if not too_soon:
                        await log_anomaly_event(api_id, response_time_ms, score, reason="rt>UCL")
                        text_warn = (
                            f"⚠️ <b>ПОПЕРЕДЖЕННЯ (аномалія): {api.name}</b>\n\n"
                            f"Час відповіді {response_time_ms} мс перевищив поріг UCL ~ {latest_metrics.ucl_ms} мс.\n"
                            f"Це може свідчити про деградацію продуктивності."
                        )
                        try:
                            for chat_id in await get_subscribers_for_api(api.id):
                                await bot.send_message(chat_id, text_warn)
                        except Exception:
                            pass
        except Exception as _:
            # Не ламаємо основний потік
            pass
    
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
            # Сповіщення усім підписникам
            try:
                for chat_id in await get_subscribers_for_api(api.id):
                    await bot.send_message(chat_id, recovery_message)
            except Exception:
                pass
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
            
            # Сповіщення усім підписникам
            text_down = f"🔴 <b>ПАДІННЯ: {api.name}</b>\n\n{format_api_status(api, update_data)}"
            try:
                for chat_id in await get_subscribers_for_api(api.id):
                    await bot.send_message(chat_id=chat_id, text=text_down)
            except Exception:
                pass

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
        # Розсилка всім глобальним підписникам (та адміну)
        try:
            for chat_id in await get_subscribers_for_api(0):
                await bot.send_message(chat_id, full_summary)
        except Exception:
            await bot.send_message(bot.config.ADMIN_USER_ID, full_summary)
        logger.info("Щоденний звіт успішно надіслано.")
    except Exception as e:
        logger.error(f"Не вдалося надіслати щоденний звіт: {e}")


async def add_job_to_scheduler(scheduler: AsyncIOScheduler, bot: Bot, api: MonitoredAPI):
    job_id = f"api_check_{api.id}"
    scheduler.add_job(
        check_api, trigger=IntervalTrigger(seconds=api.check_interval),
        args=[bot, api.id], id=job_id, name=f"Check {api.name}", replace_existing=True,
        next_run_time=datetime.datetime.utcnow() + datetime.timedelta(seconds=2)
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
    tz_name = getattr(bot.config, 'TZ', 'UTC') or 'UTC'
    report_hour = int(getattr(bot.config, 'REPORT_HOUR', 9) or 9)
    report_minute = int(getattr(bot.config, 'REPORT_MINUTE', 0) or 0)
    scheduler.add_job(send_daily_summary, 'cron', hour=report_hour, minute=report_minute, args=[bot], timezone=tz_name)
    logger.info(f"Додано завдання для щоденної розсилки о {report_hour:02d}:{report_minute:02d} ({tz_name}).")

    # Додаємо періодичний перерахунок ML метрик
    if settings.ML_ENABLED:
        interval_sec = max(60, int(settings.ML_COMPUTE_INTERVAL_MINUTES) * 60)

        async def compute_ml_job():
            try:
                apis = await get_all_active_apis()
                for api in apis:
                    hist = await get_recent_history_points(api.id, limit=int(settings.ML_WINDOW))
                    series = [int(h.response_time_ms) for h in hist if h.response_time_ms and h.response_time_ms > 0]
                    metrics = robust_stats(series)
                    payload = {
                        "window_size": len(series) or int(settings.ML_WINDOW),
                        "median_ms": int(metrics.get("median", 0) or 0),
                        "mad_ms": int(metrics.get("mad", 0) or 0),
                        "ewma_ms": int(metrics.get("ewma", 0) or 0),
                        "ucl_ms": int(metrics.get("ucl", 0) or 0),
                    }
                    await save_ml_metric(api.id, payload)
                logger.info("ML-метрики оновлено.")
            except Exception as e:
                logger.error(f"Помилка ML-обчислень: {e}")

        scheduler.add_job(compute_ml_job, trigger=IntervalTrigger(seconds=interval_sec))
        logger.info(f"Додано завдання ML-обчислень кожні {interval_sec//60} хв.")

    # Нагадування про довготривалий даун з урахуванням тихих годин
    async def down_reminder_job():
        try:
            apis = await get_all_active_apis()
            now = datetime.datetime.utcnow()
            for api in apis:
                if not api.is_up and api.incident_start_time:
                    state = await get_or_create_notification_state(api.id)
                    need = False
                    if not state.last_down_reminder_at:
                        need = True
                    else:
                        delta = now - state.last_down_reminder_at
                        need = delta.total_seconds() >= settings.DOWNTIME_REMINDER_MINUTES * 60
                    if need:
                        # Тихі години: приглушити не-критичні попередження, але для DOWN надсилати коротше
                        hour = (now.hour + 0) % 24
                        is_quiet = bool(settings.QUIET_HOURS_ENABLED) and (
                            settings.QUIET_START_HOUR > settings.QUIET_END_HOUR and (hour >= settings.QUIET_START_HOUR or hour < settings.QUIET_END_HOUR)
                            or settings.QUIET_START_HOUR < settings.QUIET_END_HOUR and (settings.QUIET_START_HOUR <= hour < settings.QUIET_END_HOUR)
                        )
                        text = f"⏰ Триває даун '{api.name}' (ID {api.id}). Тривалість: {format_timedelta(now - api.incident_start_time)}"
                        try:
                            for chat_id in await get_subscribers_for_api(api.id):
                                await bot.send_message(chat_id, text if not is_quiet else text)
                        except Exception:
                            pass
                        await update_down_reminder_time(api.id, now)
        except Exception as e:
            logger.error(f"Помилка нагадувача: {e}")

    scheduler.add_job(down_reminder_job, trigger=IntervalTrigger(seconds=300), id="down_reminder")