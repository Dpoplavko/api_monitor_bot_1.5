# src/handlers.py
# Файл з обробниками команд та повідомлень від користувача

import logging
import json
from aiogram import Router, Bot, F
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command, StateFilter, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import (add_api_to_db, get_all_apis, get_api_by_id,
                      toggle_api_monitoring, delete_api_from_db, get_stats_for_period, get_history_for_period,
                      subscribe_chat, unsubscribe_chat, get_subscribers_for_api, get_latest_ml_metric)
from scheduler import add_job_to_scheduler, remove_job_from_scheduler
from utils import (format_api_status, parse_add_command, format_statistics_report, 
                   generate_statistics_chart)
from runtime_config import get_chart_overrides, set_chart_option, get_effective_chart_config_sync

logger = logging.getLogger(__name__)
router = Router()

class AdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return message.from_user.id == settings.ADMIN_USER_ID

class AddFullApiFSM(StatesGroup):
    waiting_for_name = State()
    waiting_for_api_data = State()
    waiting_for_headers = State()
    waiting_for_body = State()

@router.message(AdminFilter(), CommandStart())
async def cmd_start(message: Message):
    await message.answer("Привіт! Я бот для моніторингу API. \n"
                         "Використовуйте /help, щоб побачити список команд.")

@router.message(AdminFilter(), Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "<b>Доступні команди:</b>\n\n"
        "<code>/add_full</code> - Покроково додати API з іменем, заголовками та тілом запиту.\n\n"
        "<code>/list_apis</code> - Показати всі API, що моніторяться.\n\n"
        "<code>/status &lt;id&gt;</code> - Детальний статус конкретного API.\n\n"
        "<code>/stats &lt;id&gt; [період]</code> - Статистика та графік за період.\n"
        "   - `період`: 1h, 6h, 12h, 24h, 7d, 30d (за замовчуванням 24h).\n\n"
        "<code>/pause_api &lt;id&gt;</code> - Призупинити моніторинг.\n\n"
        "<code>/resume_api &lt;id&gt;</code> - Відновити моніторинг.\n\n"
    "<code>/delete_api &lt;id&gt;</code> - Видалити API з моніторингу.\n\n"
    "<b>Підписки (multi-chat):</b>\n"
    "<code>/subscribe [id]</code> - Підписати цей чат на всі/конкретний API.\n"
    "<code>/unsubscribe [id]</code> - Відписати цей чат.\n"
    "<code>/subscribe_chat &lt;chat_id&gt; [id]</code> - (адмін) підписати інший чат.\n"
    "<code>/unsubscribe_chat &lt;chat_id&gt; [id]</code> - (адмін) відписати інший чат.\n\n"
    "<b>Налаштування графіків:</b>\n"
    "<code>/chart</code> - показати поточні налаштування та отримати інлайн-кнопки.\n"
    "<code>/chart set &lt;KEY&gt; &lt;VALUE&gt;</code> - змінити опцію (адмін)."
    )
    await message.answer(help_text)

def build_chart_kb(ov: dict) -> InlineKeyboardMarkup:
    eff = get_effective_chart_config_sync(ov)
    ag = (eff.get('CHART_AGGREGATION') or 'per_minute').lower()
    ys = (eff.get('CHART_Y_SCALE') or 'log').lower()
    raw_on = bool(int(eff.get('CHART_SHOW_RAW_LINE', 1)))
    an_on = bool(int(eff.get('CHART_MARK_ANOMALIES', 1)))
    ew_on = bool(int(eff.get('CHART_SHOW_EWMA', 1)))
    ucl_on = bool(int(eff.get('CHART_SHOW_UCL', 1)))

    def mark(txt, cond):
        return ("✅ " + txt) if cond else txt

    kb = [
        [
            InlineKeyboardButton(text=mark("PerMin", ag == 'per_minute'), callback_data="chart:CHART_AGGREGATION:per_minute"),
            InlineKeyboardButton(text=mark("LTTB", ag == 'lttb'), callback_data="chart:CHART_AGGREGATION:lttb"),
            InlineKeyboardButton(text=mark("None", ag == 'none'), callback_data="chart:CHART_AGGREGATION:none"),
        ],
        [
            InlineKeyboardButton(text=mark("Log", ys == 'log'), callback_data="chart:CHART_Y_SCALE:log"),
            InlineKeyboardButton(text=mark("Linear", ys == 'linear'), callback_data="chart:CHART_Y_SCALE:linear"),
            InlineKeyboardButton(text=mark("Auto", ys == 'auto'), callback_data="chart:CHART_Y_SCALE:auto"),
        ],
        [
            InlineKeyboardButton(text=mark("Raw ON", raw_on), callback_data="chart:CHART_SHOW_RAW_LINE:1"),
            InlineKeyboardButton(text=mark("Raw OFF", not raw_on), callback_data="chart:CHART_SHOW_RAW_LINE:0"),
        ],
        [
            InlineKeyboardButton(text=mark("Anom ON", an_on), callback_data="chart:CHART_MARK_ANOMALIES:1"),
            InlineKeyboardButton(text=mark("Anom OFF", not an_on), callback_data="chart:CHART_MARK_ANOMALIES:0"),
        ],
        [
            InlineKeyboardButton(text=mark("EWMA ON", ew_on), callback_data="chart:CHART_SHOW_EWMA:1"),
            InlineKeyboardButton(text=mark("EWMA OFF", not ew_on), callback_data="chart:CHART_SHOW_EWMA:0"),
        ],
        [
            InlineKeyboardButton(text=mark("UCL ON", ucl_on), callback_data="chart:CHART_SHOW_UCL:1"),
            InlineKeyboardButton(text=mark("UCL OFF", not ucl_on), callback_data="chart:CHART_SHOW_UCL:0"),
        ],
        [
            InlineKeyboardButton(text="♻️ Reset overrides", callback_data="chart:RESET:1"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.message(AdminFilter(), Command("add_full"))
async def cmd_add_full(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "<b>Крок 1/4:</b> Введіть унікальне ім'я для цього API (напр., 'Продакшн API платежів')."
    )
    await state.set_state(AddFullApiFSM.waiting_for_name)

@router.message(AdminFilter(), AddFullApiFSM.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer(
        "<b>Крок 2/4:</b> Надішліть основні дані API у форматі:\n"
        "<code>url [method] [status] [timeout] [interval] [json_keys]</code>\n\n"
        "<i>Приклад:</i> <code>[https://api.example.com](https://api.example.com) POST 201 10 60 status,data</code>"
    )
    await state.set_state(AddFullApiFSM.waiting_for_api_data)

@router.message(AdminFilter(), AddFullApiFSM.waiting_for_api_data)
async def process_full_api_data(message: Message, state: FSMContext):
    try:
        api_data = parse_add_command(message.text)
        await state.update_data(api_data=api_data)
        await message.answer(
            "<b>Крок 3/4:</b> Тепер надішліть заголовки (headers) у форматі JSON.\n\n"
            'Якщо заголовки не потрібні, просто напишіть <code>-</code> або <code>skip</code>.'
        )
        await state.set_state(AddFullApiFSM.waiting_for_headers)
    except ValueError as e:
        await message.answer(f"Помилка в даних. Спробуйте ще раз.\nДеталі: {e}")

@router.message(AdminFilter(), AddFullApiFSM.waiting_for_headers)
async def process_full_headers(message: Message, state: FSMContext):
    headers = None
    if message.text.lower() not in ['-', 'skip']:
        try:
            headers = json.loads(message.text)
            if not isinstance(headers, dict): raise json.JSONDecodeError("JSON object expected", "", 0)
        except json.JSONDecodeError:
            await message.answer("Некоректний формат JSON. Спробуйте ще раз або напишіть <code>-</code>.")
            return

    await state.update_data(headers=headers)
    await message.answer(
        "<b>Крок 4/4:</b> Надішліть тіло запиту (request body) у форматі JSON.\n\n"
        "Якщо тіло не потрібне, напишіть <code>-</code> або <code>skip</code>."
    )
    await state.set_state(AddFullApiFSM.waiting_for_body)

@router.message(AdminFilter(), AddFullApiFSM.waiting_for_body)
async def process_full_body_and_save(message: Message, state: FSMContext, bot: Bot, scheduler: AsyncIOScheduler):
    body = None
    if message.text.lower() not in ['-', 'skip']:
        try:
            body = json.loads(message.text)
        except json.JSONDecodeError:
            await message.answer("Некоректний формат JSON. Спробуйте ще раз або напишіть <code>-</code>.")
            return

    user_data = await state.get_data()
    api_data = user_data.get("api_data", {})
    api_data.update({
        'name': user_data.get('name'),
        'headers': user_data.get('headers'),
        'request_body': body
    })

    api = await add_api_to_db(api_data)
    await add_job_to_scheduler(scheduler, bot, api)
    await state.clear()
    await message.answer(f"✅ API '<b>{api.name}</b>' (ID: {api.id}) успішно додано.")

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Вкажіть ID. Приклад: <code>/stats 1 24h</code>")
        return
    try:
        api_id = int(parts[1])
        period = parts[2] if len(parts) > 2 else "24h"
    except ValueError:
        await message.answer("ID має бути числом.")
        return
        
    api = await get_api_by_id(api_id)
    if not api:
        await message.answer(f"API з ID {api_id} не знайдено.")
        return
    
    await message.answer("⏳ Генерую звіт та графік...")

    stats_data = await get_stats_for_period(api_id, period)
    if not stats_data:
        await message.answer(f"Некоректний період: {period}. Доступні: 1h, 6h, 12h, 24h, 7d, 30d.")
        return
    
    history_data = await get_history_for_period(api_id, period)
    chart_overrides = await get_chart_overrides()
    ml_metric = await get_latest_ml_metric(api_id)
    chart_buffer = await generate_statistics_chart(
        history_data, api.name, period, stats_data.get("avg_response_time_ms", 0), (ml_metric.ucl_ms if ml_metric and ml_metric.ucl_ms else None), chart_overrides
    )
    
    ml_part = None
    if ml_metric:
        ml_part = {
            "median_ms": ml_metric.median_ms,
            "mad_ms": ml_metric.mad_ms,
            "ewma_ms": ml_metric.ewma_ms,
            "ucl_ms": ml_metric.ucl_ms,
            "window": ml_metric.window_size,
        }
    report_caption = format_statistics_report(api.name, stats_data, ml_part)
    
    await message.reply_photo(
        photo=BufferedInputFile(chart_buffer.read(), filename=f"stats_{api_id}_{period}.png"),
        caption=report_caption
    )

@router.message(AdminFilter(), Command("chart"))
async def cmd_chart(message: Message):
    parts = message.text.split()
    if len(parts) >= 2 and parts[1].lower() == 'set':
        if len(parts) < 4:
            await message.answer("Використання: /chart set KEY VALUE")
            return
        key, value = parts[2], " ".join(parts[3:])
        try:
            await set_chart_option(key.upper(), value)
            await message.answer(f"✅ Збережено {key} = {value} (діє для нових графіків)")
        except ValueError as e:
            await message.answer(f"Помилка: {e}")
        return

    ov = await get_chart_overrides()
    rows = [f"<code>{k}</code> = <b>{v}</b>" for k, v in sorted(ov.items())]
    if not rows:
        rows.append("(використовуються значення з .env)")
    text = "<b>Поточні налаштування графіків</b>\n" + "\n".join(rows)
    await message.answer(text, reply_markup=build_chart_kb(ov))

@router.callback_query(F.data.startswith("chart:"))
async def cb_chart(call: CallbackQuery):
    try:
        _, key, value = call.data.split(":", 2)
    except Exception:
        await call.answer("Невірні дані", show_alert=True)
        return
    if key == 'RESET':
        # Clear common keys
        for k in [
            'CHART_AGGREGATION','CHART_Y_SCALE','CHART_SHOW_RAW_LINE','CHART_MARK_ANOMALIES',
            'CHART_SHOW_EWMA','CHART_SHOW_UCL']:
            try:
                await set_chart_option(k, None)
            except Exception:
                pass
        await call.answer("Скинуто до .env")
    else:
        try:
            await set_chart_option(key, value)
            await call.answer("Збережено")
        except ValueError as e:
            await call.answer(str(e), show_alert=True)
            return
    ov = await get_chart_overrides()
    rows = [f"<code>{k}</code> = <b>{v}</b>" for k, v in sorted(ov.items())]
    if not rows:
        rows.append("(використовуються значення з .env)")
    text = "<b>Поточні налаштування графіків</b>\n" + "\n".join(rows)
    try:
        await call.message.edit_text(text, reply_markup=build_chart_kb(ov))
    except Exception:
        await call.message.reply(text, reply_markup=build_chart_kb(ov))

@router.message(AdminFilter(), Command("list_apis"))
async def cmd_list_apis(message: Message):
    apis = await get_all_apis()
    if not apis:
        await message.answer("Список API для моніторингу порожній.")
        return

    response_text = "<b>API, що моніторяться:</b>\n\n"
    for api in apis:
        status_icon = "🟢" if api.is_up else "🔴"
        active_icon = "▶️" if api.is_active else "⏸️"
        response_text += f"{active_icon} {status_icon} <b>{api.name}</b> (ID: <code>{api.id}</code>)\n"
    await message.answer(response_text)

@router.message(AdminFilter(), Command("status"))
async def cmd_status(message: Message):
    try:
        api_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer("Вкажіть ID. Приклад: <code>/status 1</code>")
        return
    api = await get_api_by_id(api_id)
    if not api:
        await message.answer(f"API з ID {api_id} не знайдено.")
        return
    # Inline кнопки для швидких дій
    kb = InlineKeyboardMarkup(inline_keyboard=[[ 
        InlineKeyboardButton(text="📊 Статистика 24h", callback_data=f"stats:{api.id}:24h"),
        InlineKeyboardButton(text=("⏸️ Пауза" if api.is_active else "▶️ Відновити"), callback_data=(f"pause:{api.id}" if api.is_active else f"resume:{api.id}")),
    ],[
        InlineKeyboardButton(text="🔔 Підписатися", callback_data=f"sub:{api.id}"),
        InlineKeyboardButton(text="🔕 Відписатися", callback_data=f"unsub:{api.id}"),
    ]])
    await message.answer(format_api_status(api), reply_markup=kb)

async def _toggle_api(message: Message, scheduler: AsyncIOScheduler, is_active: bool):
    command_name = "resume_api" if is_active else "pause_api"
    try:
        api_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer(f"Вкажіть ID. Приклад: <code>/{command_name} 1</code>")
        return

    api = await toggle_api_monitoring(api_id, is_active)
    if not api:
        await message.answer(f"API з ID {api_id} не знайдено.")
        return

    if is_active:
        await add_job_to_scheduler(scheduler, message.bot, api)
        await message.answer(f"▶️ Моніторинг API '<b>{api.name}</b>' (ID: {api.id}) відновлено.")
    else:
        remove_job_from_scheduler(scheduler, api_id)
        await message.answer(f"⏸️ Моніторинг API '<b>{api.name}</b>' (ID: {api.id}) призупинено.")

@router.message(AdminFilter(), Command("pause_api"))
async def cmd_pause_api(message: Message, scheduler: AsyncIOScheduler):
    await _toggle_api(message, scheduler, is_active=False)

@router.message(AdminFilter(), Command("resume_api"))
async def cmd_resume_api(message: Message, scheduler: AsyncIOScheduler):
    await _toggle_api(message, scheduler, is_active=True)

@router.message(AdminFilter(), Command("delete_api"))
async def cmd_delete_api(message: Message, scheduler: AsyncIOScheduler):
    try:
        api_id = int(message.text.split()[1])
    except (IndexError, ValueError):
        await message.answer("Вкажіть ID. Приклад: <code>/delete_api 1</code>")
        return
    
    api = await get_api_by_id(api_id)
    if not api:
        await message.answer(f"API з ID {api_id} не знайдено.")
        return
    
    api_name = api.name
    deleted = await delete_api_from_db(api_id)
    if deleted:
        remove_job_from_scheduler(scheduler, api_id)
        await message.answer(f"🗑️ API '<b>{api_name}</b>' (ID: {api_id}) видалено.")
    else:
        await message.answer(f"Не вдалося видалити API з ID {api_id}.")

# --- Підписки ---
@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    parts = message.text.split()
    api_id = None
    if len(parts) > 1:
        try:
            api_id = int(parts[1])
        except ValueError:
            await message.answer("ID має бути числом або пропустіть, щоб підписатися на всі API.")
            return
    ok = await subscribe_chat(message.chat.id, api_id)
    if ok:
        await message.answer("✅ Підписка оформлена.")
    else:
        await message.answer("ℹ️ Ви вже підписані або сталася помилка.")

@router.message(Command("unsubscribe"))
async def cmd_unsubscribe(message: Message):
    parts = message.text.split()
    api_id = None
    if len(parts) > 1:
        try:
            api_id = int(parts[1])
        except ValueError:
            await message.answer("ID має бути числом або пропустіть, щоб відписатися від глобальної підписки.")
            return
    ok = await unsubscribe_chat(message.chat.id, api_id)
    await message.answer("✅ Відписка виконана." if ok else "ℹ️ Підписку не знайдено.")

@router.message(AdminFilter(), Command("subscribe_chat"))
async def cmd_subscribe_chat(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Використання: /subscribe_chat <chat_id> [api_id]")
        return
    try:
        chat_id = int(parts[1])
        api_id = int(parts[2]) if len(parts) > 2 else None
    except ValueError:
        await message.answer("chat_id та api_id мають бути числами")
        return
    ok = await subscribe_chat(chat_id, api_id)
    await message.answer("✅ Підписано." if ok else "ℹ️ Вже підписаний/помилка.")

@router.message(AdminFilter(), Command("unsubscribe_chat"))
async def cmd_unsubscribe_chat(message: Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Використання: /unsubscribe_chat <chat_id> [api_id]")
        return
    try:
        chat_id = int(parts[1])
        api_id = int(parts[2]) if len(parts) > 2 else None
    except ValueError:
        await message.answer("chat_id та api_id мають бути числами")
        return
    ok = await unsubscribe_chat(chat_id, api_id)
    await message.answer("✅ Відписано." if ok else "ℹ️ Підписку не знайдено.")

# --- Callback-и для inline кнопок ---
@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery):
    try:
        _, id_str, period = call.data.split(':')
        api_id = int(id_str)
    except Exception:
        await call.answer("Невірні дані", show_alert=True)
        return
    api = await get_api_by_id(api_id)
    if not api:
        await call.answer("API не знайдено", show_alert=True)
        return
    stats_data = await get_stats_for_period(api_id, period)
    history_data = await get_history_for_period(api_id, period)
    ml_metric = await get_latest_ml_metric(api_id)
    chart_overrides = await get_chart_overrides()
    chart_buffer = await generate_statistics_chart(
        history_data, api.name, period, stats_data.get("avg_response_time_ms", 0), (ml_metric.ucl_ms if ml_metric and ml_metric.ucl_ms else None), chart_overrides
    )
    ml_part = None
    if ml_metric:
        ml_part = {"median_ms": ml_metric.median_ms, "mad_ms": ml_metric.mad_ms, "ewma_ms": ml_metric.ewma_ms, "ucl_ms": ml_metric.ucl_ms, "window": ml_metric.window_size}
    caption = format_statistics_report(api.name, stats_data, ml_part)
    await call.message.reply_photo(photo=BufferedInputFile(chart_buffer.read(), filename=f"stats_{api_id}_{period}.png"), caption=caption)
    await call.answer()

@router.callback_query(F.data.startswith("pause:"))
async def cb_pause(call: CallbackQuery, scheduler: AsyncIOScheduler):
    api_id = int(call.data.split(":")[1])
    await toggle_api_monitoring(api_id, False)
    remove_job_from_scheduler(scheduler, api_id)
    await call.answer("Поставлено на паузу")

@router.callback_query(F.data.startswith("resume:"))
async def cb_resume(call: CallbackQuery, scheduler: AsyncIOScheduler):
    api_id = int(call.data.split(":")[1])
    api = await toggle_api_monitoring(api_id, True)
    if api:
        await add_job_to_scheduler(scheduler, call.bot, api)
    await call.answer("Відновлено")

@router.callback_query(F.data.startswith("sub:"))
async def cb_sub(call: CallbackQuery):
    api_id = int(call.data.split(":")[1])
    await subscribe_chat(call.message.chat.id, api_id)
    await call.answer("Підписано")

@router.callback_query(F.data.startswith("unsub:"))
async def cb_unsub(call: CallbackQuery):
    api_id = int(call.data.split(":")[1])
    await unsubscribe_chat(call.message.chat.id, api_id)
    await call.answer("Відписано")

@router.message(AdminFilter(), StateFilter(None))
async def unknown_command(message: Message):
    await message.answer("Невідома команда. Використовуйте /help для списку команд.")