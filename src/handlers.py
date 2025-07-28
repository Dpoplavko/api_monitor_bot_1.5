# src/handlers.py
# Файл з обробниками команд та повідомлень від користувача

import logging
import json
from aiogram import Router, Bot, F
from aiogram.types import Message, BufferedInputFile
from aiogram.filters import CommandStart, Command, StateFilter, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from config import settings
from database import (add_api_to_db, get_all_apis, get_api_by_id,
                      toggle_api_monitoring, delete_api_from_db, get_stats_for_period, get_history_for_period)
from scheduler import add_job_to_scheduler, remove_job_from_scheduler
from utils import (format_api_status, parse_add_command, format_statistics_report, 
                   generate_statistics_chart)

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
        "<code>/delete_api &lt;id&gt;</code> - Видалити API з моніторингу."
    )
    await message.answer(help_text)

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

@router.message(AdminFilter(), Command("stats"))
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
    chart_buffer = await generate_statistics_chart(
        history_data, api.name, period, stats_data.get("avg_response_time_ms", 0)
    )
    
    report_caption = format_statistics_report(api.name, stats_data)
    
    await message.reply_photo(
        photo=BufferedInputFile(chart_buffer.read(), filename=f"stats_{api_id}_{period}.png"),
        caption=report_caption
    )

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
    await message.answer(format_api_status(api))

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

@router.message(AdminFilter(), StateFilter(None))
async def unknown_command(message: Message):
    await message.answer("Невідома команда. Використовуйте /help для списку команд.")