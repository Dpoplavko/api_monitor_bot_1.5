# src/handlers.py
# Файл з обробниками команд та повідомлень від користувача

import logging
import json
from typing import Any, Optional, cast
from aiogram import Router, Bot, F
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart, Command, StateFilter, BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore

from config import settings
from database import (add_api_to_db, get_all_apis, get_api_by_id,  # type: ignore
                      toggle_api_monitoring, delete_api_from_db, get_stats_for_period, get_history_for_period,  # type: ignore
                      subscribe_chat, unsubscribe_chat, get_latest_ml_metric,  # type: ignore
                      update_api_fields, set_api_mute, set_anomaly_alerts, is_chat_subscribed,  # type: ignore
                      get_anomaly_stats_for_period, set_anomaly_params,  # type: ignore
                      is_chat_anomaly_notifications_enabled, set_chat_anomaly_notifications)  # type: ignore
from scheduler import add_job_to_scheduler, remove_job_from_scheduler, check_api
from utils import (format_api_status, parse_add_command, format_statistics_report,  # type: ignore
                   generate_statistics_chart)  # type: ignore
from runtime_config import get_chart_overrides, set_chart_option, get_effective_chart_config_sync
from version import VERSION, RELEASE_NOTES
from sysmon import format_server_status  # type: ignore

logger = logging.getLogger(__name__)
router = Router()

async def _guard_message_access(message: Message) -> bool:
    try:
        chat_type = getattr(message.chat, 'type', 'private')
        user_id = getattr(message.from_user, 'id', None)
    except Exception:
        return True
    if chat_type != 'private' and user_id != settings.ADMIN_USER_ID:
        try:
            bot = getattr(message, 'bot', None)
            username = None
            if bot is not None:
                me = await bot.get_me()
                username = getattr(me, 'username', None)
            hint = "Скористайтесь ботом у приватному чаті з посиланням нижче."
            if username:
                link = f"https://t.me/{username}"
                hint += f"\nВідкрити: {link}"
            await message.reply(hint, disable_web_page_preview=True)
        except Exception:
            pass
        return False
    return True

async def _guard_callback_access(call: CallbackQuery) -> bool:
    try:
        chat_type = getattr(call.message.chat, 'type', 'private') if call.message else 'private'
        user_id = getattr(call.from_user, 'id', None)
    except Exception:
        return True
    if chat_type != 'private' and user_id != settings.ADMIN_USER_ID:
        try:
            bot = getattr(call, 'bot', None)
            username = None
            if bot is not None:
                me = await bot.get_me()
                username = getattr(me, 'username', None)
            text = "Використовуйте бота у приватному чаті."
            if username:
                text += f"\nВідкрити: https://t.me/{username}"
            await call.answer(text, show_alert=True)
        except Exception:
            pass
        return False
    return True

# --- Safe helpers for editing messages from callbacks (handle inline queries with no message) ---
async def _safe_edit_text(call: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        msg = getattr(call, 'message', None)
        if msg is not None:
            await msg.edit_text(text, reply_markup=reply_markup)
            return True
        # Inline-only: no message context; cannot edit. Show an alert instead.
        await call.answer("Відкрийте бота у чаті, щоб побачити повідомлення.", show_alert=True)
    except Exception:
        # Silently ignore UI edit failures
        pass
    return False

async def _safe_edit_reply_markup(call: CallbackQuery, reply_markup: InlineKeyboardMarkup | None = None) -> bool:
    try:
        msg = getattr(call, 'message', None)
        if msg is not None:
            await msg.edit_reply_markup(reply_markup=reply_markup)
            return True
        await call.answer("Відкрийте бота у чаті, щоб побачити повідомлення.", show_alert=True)
    except Exception:
        pass
    return False

def _get_chat_id_from_call(call: CallbackQuery) -> Optional[int]:
    try:
        if call.message and call.message.chat:
            return call.message.chat.id
    except Exception:
        return None
    return None

class AdminFilter(BaseFilter):
    async def __call__(self, message: Message) -> bool:
        return getattr(message.from_user, 'id', None) == settings.ADMIN_USER_ID

class AddFullApiFSM(StatesGroup):
    waiting_for_name = State()
    waiting_for_api_data = State()
    waiting_for_headers = State()
    waiting_for_body = State()

class EditApiFSM(StatesGroup):
    waiting_for_value = State()

class CreateApiFSM(StatesGroup):
    waiting_for_value = State()

@router.message(CommandStart())
async def cmd_start(message: Message):
    if not await _guard_message_access(message):
        return
    from database import is_chat_subscribed
    is_sub = await is_chat_subscribed(message.chat.id, None)
    is_admin = getattr(message.from_user, 'id', None) == settings.ADMIN_USER_ID
    anom_on = await is_chat_anomaly_notifications_enabled(message.chat.id)
    await message.answer("Привіт! Ось меню:", reply_markup=build_main_menu(is_sub, is_admin, anom_on))

@router.message(Command("help"))
async def cmd_help(message: Message):
    if not await _guard_message_access(message):
        return
    from database import is_chat_subscribed
    is_sub = await is_chat_subscribed(message.chat.id, None)
    is_admin = getattr(message.from_user, 'id', None) == settings.ADMIN_USER_ID
    anom_on = await is_chat_anomaly_notifications_enabled(message.chat.id)
    await message.answer("Для зручності користуйтесь інлайн-меню нижче.", reply_markup=build_main_menu(is_sub, is_admin, anom_on))

def _format_api_row(api: Any) -> str:
    status_icon = "🟢" if bool(getattr(api, 'is_up', False)) else "🔴"
    active_icon = "▶️" if bool(getattr(api, 'is_active', False)) else "⏸️"
    return f"{active_icon} {status_icon} <b>{api.name}</b> (ID: <code>{api.id}</code>)"

def build_main_menu(is_sub: bool, is_admin: bool = False, anom_on: bool = True) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if is_admin:
        rows.append([InlineKeyboardButton(text="➕ Додати монітор", callback_data="add")])
    rows.append([InlineKeyboardButton(text="📋 Монітори", callback_data="apis")])
    rows.append([InlineKeyboardButton(text="📈 Налаштування графіків", callback_data="chart_menu")])
    rows.append([InlineKeyboardButton(text="ℹ️ Можливості та версія", callback_data="features")])
    if is_sub:
        rows.append([InlineKeyboardButton(text="🔕 Відписатися від усіх", callback_data="unsub_all")])
    else:
        rows.append([InlineKeyboardButton(text="🔔 Підписатися на всі", callback_data="sub_all")])
    rows.append([InlineKeyboardButton(text=("⚠️ Аномалії для мене: ON" if anom_on else "⚠️ Аномалії для мене: OFF"), callback_data="toggle_user_anom")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="🖥️ Статус сервера", callback_data="server_status")])
        rows.append([InlineKeyboardButton(text="📡 Metrics health", callback_data="metrics_health")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_api_panel(api: Any, chat_id: Optional[int], user_id: int | None = None) -> InlineKeyboardMarkup:
    is_admin = (user_id == settings.ADMIN_USER_ID)
    # state toggles
    pause_btn = InlineKeyboardButton(text=("⏸️ Пауза" if api.is_active else "▶️ Відновити"), callback_data=(f"pause:{api.id}" if api.is_active else f"resume:{api.id}"))
    stats_btn = InlineKeyboardButton(text="📊 24h", callback_data=f"stats:{api.id}:24h")
    stats1h_btn = InlineKeyboardButton(text="1h", callback_data=f"stats:{api.id}:1h")
    stats6_btn = InlineKeyboardButton(text="6h", callback_data=f"stats:{api.id}:6h")
    stats7d_btn = InlineKeyboardButton(text="7d", callback_data=f"stats:{api.id}:7d")
    stats30d_btn = InlineKeyboardButton(text="30d", callback_data=f"stats:{api.id}:30d")
    # Mute / Unmute
    muted = bool(getattr(api, 'notifications_muted', False))
    mute_btn = InlineKeyboardButton(text=("🔔 Unmute" if muted else "🔕 Mute"), callback_data=(f"unmute:{api.id}" if muted else f"mute:{api.id}"))
    # Anomaly alerts toggle
    an_enabled = bool(getattr(api, 'anomaly_alerts_enabled', True))
    an_btn = InlineKeyboardButton(text=("⚠️ Аномалії ON" if an_enabled else "⚠️ Аномалії OFF"), callback_data=f"anom:{api.id}:{0 if an_enabled else 1}")
    # Anomaly tuning summary
    sens = getattr(api, 'anomaly_sensitivity', '1.5') or '1.5'
    try:
        mm = int(getattr(api, 'anomaly_m', 3) or 3)
        nn = int(getattr(api, 'anomaly_n', 5) or 5)
    except Exception:
        mm, nn = 3, 5
    an_cfg_btn = InlineKeyboardButton(text=f"⚙️ sens {sens} · {mm}/{nn}", callback_data=f"anom_cfg:{api.id}")
    # Subscription to specific API
    sub_btn = InlineKeyboardButton(text="🔔 Підписатися", callback_data=f"sub:{api.id}")
    unsub_btn = InlineKeyboardButton(text="🔕 Відписатися", callback_data=f"unsub:{api.id}")
    # Admin-only buttons
    edit_btn = InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"edit:{api.id}")
    del_btn = InlineKeyboardButton(text="🗑️ Видалити", callback_data=f"del:{api.id}")
    back_btn = InlineKeyboardButton(text="⬅️ Назад", callback_data="apis")
    home_btn = InlineKeyboardButton(text="🏠 Меню", callback_data="menu")

    check_now_btn = InlineKeyboardButton(text="🔄 Перевірити зараз", callback_data=f"check:{api.id}")
    rows = [[stats1h_btn, stats6_btn, stats_btn, stats7d_btn, stats30d_btn]]
    if is_admin:
        rows += [
            [pause_btn, check_now_btn],
            [mute_btn, an_btn],
            [an_cfg_btn],
            [edit_btn, del_btn],
        ]
    rows += [[sub_btn, unsub_btn], [back_btn, home_btn]]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_create_api_panel(draft: dict[str, Any]) -> InlineKeyboardMarkup:
    name = draft.get('name')
    url = draft.get('url')
    method = draft.get('method', 'GET')
    status = draft.get('expected_status', 200)
    timeout = draft.get('timeout', 10)
    interval = draft.get('check_interval', 60)
    json_keys = draft.get('json_keys') or '—'
    rows = [
        [InlineKeyboardButton(text=f"Назва: {name or '—'}", callback_data="createf:name")],
        [InlineKeyboardButton(text=f"URL: {url or '—'}", callback_data="createf:url")],
        [InlineKeyboardButton(text=f"Метод: {method}", callback_data="create_method_menu")],
        [InlineKeyboardButton(text=f"Очік. статус: {status}", callback_data="create_status_menu")],
        [
            InlineKeyboardButton(text=f"Таймаут: {timeout}s", callback_data="create_timeout_menu"),
            InlineKeyboardButton(text=f"Інтервал: {interval}s", callback_data="create_interval_menu"),
        ],
        [InlineKeyboardButton(text=f"JSON Keys: {json_keys}", callback_data="createf:json_keys")],
        [
            InlineKeyboardButton(text="Заголовки (JSON)", callback_data="createf:headers"),
            InlineKeyboardButton(text="Body (JSON)", callback_data="createf:request_body"),
        ],
        [InlineKeyboardButton(text="✅ Зберегти", callback_data="create_save"), InlineKeyboardButton(text="❌ Скасувати", callback_data="create_cancel")],
        [InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_mute_menu(api_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="1 год", callback_data=f"mute_set:{api_id}:1h"), InlineKeyboardButton(text="8 год", callback_data=f"mute_set:{api_id}:8h")],
        [InlineKeyboardButton(text="24 год", callback_data=f"mute_set:{api_id}:24h"), InlineKeyboardButton(text="Назавжди", callback_data=f"mute_set:{api_id}:forever")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"api:{api_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def build_anom_menu(api_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Sens 1.2", callback_data=f"aset:{api_id}:sens:1.2"), InlineKeyboardButton(text="1.5", callback_data=f"aset:{api_id}:sens:1.5"), InlineKeyboardButton(text="1.8", callback_data=f"aset:{api_id}:sens:1.8")],
        [InlineKeyboardButton(text="m/n 2/3", callback_data=f"aset:{api_id}:mon:2:3"), InlineKeyboardButton(text="3/5", callback_data=f"aset:{api_id}:mon:3:5"), InlineKeyboardButton(text="5/8", callback_data=f"aset:{api_id}:mon:5:8")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"api:{api_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if not await _guard_message_access(message):
        return
    is_sub = await is_chat_subscribed(message.chat.id, None)
    is_admin = getattr(message.from_user, 'id', None) == settings.ADMIN_USER_ID
    anom_on = await is_chat_anomaly_notifications_enabled(message.chat.id)
    await message.answer("Меню", reply_markup=build_main_menu(is_sub, is_admin, anom_on))

@router.message(Command("features"))
async def cmd_features(message: Message):
    if not await _guard_message_access(message):
        return
    is_admin = getattr(message.from_user, 'id', None) == settings.ADMIN_USER_ID
    capabilities = [
        "• Підписка/відписка на всі або окремі монітори",
        "• Перегляд статусу та графіків (6h/24h/7d/30d)",
        "• Персональне вимкнення попереджень про аномалії",
        "• Тихі години та щоденні зведення",
    ]
    if is_admin:
        capabilities += [
            "• Створення/редагування/пауза/видалення моніторів",
            "• Ручна перевірка зараз",
            "• Налаштування детектора аномалій (sensitivity, m/n)",
        ]
    text = (
        f"🧭 <b>Можливості бота</b> (v{VERSION})\n\n" + "\n".join(capabilities) + "\n\n" + RELEASE_NOTES
    )
    await message.answer(text)

@router.message(Command("whatsnew"))
async def cmd_whatsnew(message: Message):
    if not await _guard_message_access(message):
        return
    await message.answer(RELEASE_NOTES)

@router.message(AdminFilter(), Command("announce_whatsnew"))
async def cmd_announce_whatsnew(message: Message):
    """Адмін-команда для ручної розсилки реліз-нотаток усім підписаним чатам."""
    try:
        from database import get_all_subscribed_chats, was_version_announced, mark_version_announced
        from version import VERSION, RELEASE_NOTES
        chats = await get_all_subscribed_chats()
        sent = 0
        for chat_id in chats:
            try:
                if not await was_version_announced(int(chat_id), VERSION):
                    bot_obj = getattr(message, 'bot', None) or getattr(message, 'bot', None)
                    if bot_obj:
                        await bot_obj.send_message(int(chat_id), RELEASE_NOTES)
                    await mark_version_announced(int(chat_id), VERSION)
                    sent += 1
            except Exception:
                pass
        await message.answer(f"Реліз-нотатки розіслано ({sent} чатів).")
    except Exception as e:
        await message.answer(f"Помилка розсилки: {e}")

@router.callback_query(F.data == "apis")
async def cb_list_apis(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    apis = await get_all_apis()
    if not apis:
        await _safe_edit_text(call, "Список API порожній.")
        await call.answer()
        return
    kb_rows: list[list[InlineKeyboardButton]] = []
    for api in apis[:50]:
        kb_rows.append([InlineKeyboardButton(text=f"{api.name} (ID {api.id})", callback_data=f"api:{api.id}")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")])
    await _safe_edit_text(call, "Оберіть монітор:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await call.answer()

@router.callback_query(F.data == "server_status")
async def cb_server_status(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    if getattr(call.from_user, 'id', None) != settings.ADMIN_USER_ID:
        await call.answer("Тільки адмін", show_alert=True)
        return
    # Optional service health: try to fetch metrics endpoint
    health_line = None
    try:
        import os
        import httpx  # type: ignore
        port = int(os.getenv("METRICS_PORT", "8080"))
        url = f"http://127.0.0.1:{port}/"
        async with httpx.AsyncClient(timeout=1.0) as client:  # type: ignore
            r = await client.get(url)
            if r.status_code == 200:
                health_line = "✅ Сервіс (metrics) відповідає"
            else:
                health_line = f"⚠️ Сервіс (metrics) статус: {r.status_code}"
    except Exception:
        health_line = "⚠️ Сервіс (metrics) недоступний"
    text = await format_server_status(health_line)
    await _safe_edit_text(call, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")]]))
    try:
        await call.answer()
    except Exception:
        pass

@router.message(AdminFilter(), Command("server_status"))
async def cmd_server_status(message: Message):
    if not await _guard_message_access(message):
        return
    # Metrics endpoint health
    health_line = None
    try:
        import os
        import httpx  # type: ignore
        port = int(os.getenv("METRICS_PORT", "8080"))
        url = f"http://127.0.0.1:{port}/"
        async with httpx.AsyncClient(timeout=1.0) as client:  # type: ignore
            r = await client.get(url)
            if r.status_code == 200:
                health_line = "✅ Сервіс (metrics) відповідає"
            else:
                health_line = f"⚠️ Сервіс (metrics) статус: {r.status_code}"
    except Exception:
        health_line = "⚠️ Сервіс (metrics) недоступний"
    text = await format_server_status(health_line)
    await message.answer(text)

@router.callback_query(F.data == "metrics_health")
async def cb_metrics_health(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    if getattr(call.from_user, 'id', None) != settings.ADMIN_USER_ID:
        await call.answer("Тільки адмін", show_alert=True)
        return
    # Measure latency and status code
    import os, time
    try:
        import httpx  # type: ignore
        port = int(os.getenv("METRICS_PORT", "8080"))
        url = f"http://127.0.0.1:{port}/"
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=2.0) as client:  # type: ignore
            r = await client.get(url)
            dt_ms = int((time.perf_counter() - t0) * 1000)
            body_snip = (r.text[:200] + '…') if len(r.text) > 200 else r.text
            text = (
                f"📡 <b>Metrics health</b>\n"
                f"URL: {url}\n"
                f"Status: {r.status_code}\n"
                f"Latency: {dt_ms} ms\n\n"
                f"Preview:\n<pre>{body_snip.replace('<', '&lt;')}</pre>"
            )
    except Exception as e:
        text = f"📡 <b>Metrics health</b>\nПомилка: {e}"
    await _safe_edit_text(call, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")]]))
    try:
        await call.answer()
    except Exception:
        pass

@router.callback_query(F.data == "menu")
async def cb_menu(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    chat_id = _get_chat_id_from_call(call)
    if chat_id is None:
        await call.answer("Відкрийте бота у чаті, щоб побачити меню.", show_alert=True)
        return
    is_sub = await is_chat_subscribed(chat_id, None)
    is_admin = getattr(call.from_user, 'id', None) == settings.ADMIN_USER_ID
    anom_on = await is_chat_anomaly_notifications_enabled(chat_id)
    await _safe_edit_text(call, "Меню", reply_markup=build_main_menu(is_sub, is_admin, anom_on))
    await call.answer()

@router.callback_query(F.data == "features")
async def cb_features(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    is_admin = getattr(call.from_user, 'id', None) == settings.ADMIN_USER_ID
    capabilities = [
        "• Підписка/відписка на всі або окремі монітори",
        "• Перегляд статусу та графіків (6h/24h/7d/30d)",
        "• Персональне вимкнення попереджень про аномалії",
        "• Тихі години та щоденні зведення",
    ]
    if is_admin:
        capabilities += [
            "• Створення/редагування/пауза/видалення моніторів",
            "• Ручна перевірка зараз",
            "• Налаштування детектора аномалій (sensitivity, m/n)",
        ]
    text = (
        f"🧭 <b>Можливості бота</b> (v{VERSION})\n\n" + "\n".join(capabilities) + "\n\n" + RELEASE_NOTES
    )
    await _safe_edit_text(call, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Меню", callback_data="menu")]]))
    try:
        await call.answer()
    except Exception:
        pass

@router.callback_query(F.data == "toggle_user_anom")
async def cb_toggle_user_anom(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    chat_id = call.message.chat.id if call.message else getattr(call.from_user, 'id', 0)
    cur = await is_chat_anomaly_notifications_enabled(chat_id)
    await set_chat_anomaly_notifications(chat_id, not cur)
    # Rebuild menu to reflect state
    is_sub = await is_chat_subscribed(chat_id, None)
    is_admin = getattr(call.from_user, 'id', None) == settings.ADMIN_USER_ID
    anom_on = await is_chat_anomaly_notifications_enabled(chat_id)
    await _safe_edit_text(call, "Меню", reply_markup=build_main_menu(is_sub, is_admin, anom_on))
    try:
        await call.answer("Налаштування оновлено")
    except Exception:
        pass

@router.callback_query(F.data == "add")
async def cb_add(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    if call.from_user.id != settings.ADMIN_USER_ID:
        await call.answer("Тільки адмін", show_alert=True)
        return
    # Початкова чернетка
    draft: dict[str, Any] = {
        'name': None, 'url': None, 'method': 'GET', 'expected_status': 200,
        'timeout': 10, 'check_interval': 60, 'json_keys': None, 'headers': None, 'request_body': None
    }
    await state.update_data(create_draft=draft)
    if not await _safe_edit_text(call, "Створення монітора (чернетка)", reply_markup=build_create_api_panel(draft)):
        await call.answer("Відкрийте бота у чаті, щоб створювати монітори.", show_alert=True)
    await call.answer()

@router.callback_query(F.data == "create_method_menu")
async def cb_create_method_menu(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=m, callback_data=f"create_method:{m}") for m in ["GET","POST","PUT","DELETE","PATCH"]],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="create_back")]
    ])
    await _safe_edit_reply_markup(call, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("create_method:"))
async def cb_create_method(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    parts = (call.data or '').split(":")
    if len(parts) < 2:
        await call.answer("Невірні дані", show_alert=True)
        return
    method = parts[1]
    data = await state.get_data()
    draft = data.get('create_draft', {})
    draft['method'] = method
    await state.update_data(create_draft=draft)
    await _safe_edit_reply_markup(call, reply_markup=build_create_api_panel(draft))
    await call.answer("Збережено")

@router.callback_query(F.data == "create_status_menu")
async def cb_create_status_menu(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=str(c), callback_data=f"create_status:{c}") for c in [200,201,204,400,401,404,500]],
        [InlineKeyboardButton(text="Інше…", callback_data="createf:expected_status")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="create_back")]
    ])
    await _safe_edit_reply_markup(call, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("create_status:"))
async def cb_create_status(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    parts = (call.data or '').split(":")
    if len(parts) < 2:
        await call.answer("Невірні дані", show_alert=True)
        return
    code = int(parts[1])
    data = await state.get_data()
    draft = data.get('create_draft', {})
    draft['expected_status'] = code
    await state.update_data(create_draft=draft)
    await _safe_edit_reply_markup(call, reply_markup=build_create_api_panel(draft))
    await call.answer("Збережено")

@router.callback_query(F.data == "create_timeout_menu")
async def cb_create_timeout_menu(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{s}s", callback_data=f"create_timeout:{s}") for s in [5,10,20,30]],
        [InlineKeyboardButton(text="Інше…", callback_data="createf:timeout")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="create_back")]
    ])
    await _safe_edit_reply_markup(call, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("create_timeout:"))
async def cb_create_timeout(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    parts = (call.data or '').split(":")
    if len(parts) < 2:
        await call.answer("Невірні дані", show_alert=True)
        return
    sec = int(parts[1])
    data = await state.get_data()
    draft = data.get('create_draft', {})
    draft['timeout'] = sec
    await state.update_data(create_draft=draft)
    await _safe_edit_reply_markup(call, reply_markup=build_create_api_panel(draft))
    await call.answer("Збережено")

@router.callback_query(F.data == "create_interval_menu")
async def cb_create_interval_menu(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{s}s", callback_data=f"create_interval:{s}") for s in [30,60,120,300]],
        [InlineKeyboardButton(text="Інше…", callback_data="createf:check_interval")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="create_back")]
    ])
    await _safe_edit_reply_markup(call, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("create_interval:"))
async def cb_create_interval(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    parts = (call.data or '').split(":")
    if len(parts) < 2:
        await call.answer("Невірні дані", show_alert=True)
        return
    sec = int(parts[1])
    data = await state.get_data()
    draft = data.get('create_draft', {})
    draft['check_interval'] = sec
    await state.update_data(create_draft=draft)
    await _safe_edit_reply_markup(call, reply_markup=build_create_api_panel(draft))
    await call.answer("Збережено")

@router.callback_query(F.data == "create_back")
async def cb_create_back(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    data = await state.get_data()
    draft = data.get('create_draft', {})
    await _safe_edit_reply_markup(call, reply_markup=build_create_api_panel(draft))
    await call.answer()

@router.callback_query(F.data == "create_cancel")
async def cb_create_cancel(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    await state.clear()
    chat_id = _get_chat_id_from_call(call)
    if chat_id is None:
        await call.answer("Скасовано. Відкрийте бота у чаті, щоб побачити меню.", show_alert=True)
        return
    is_sub = await is_chat_subscribed(chat_id, None)
    is_admin = getattr(call.from_user, 'id', None) == settings.ADMIN_USER_ID
    anom_on = await is_chat_anomaly_notifications_enabled(chat_id)
    await _safe_edit_text(call, "Меню", reply_markup=build_main_menu(is_sub, is_admin, anom_on))
    await call.answer("Скасовано")

@router.callback_query(F.data == "create_save")
async def cb_create_save(call: CallbackQuery, state: FSMContext, scheduler: AsyncIOScheduler):
    if not await _guard_callback_access(call):
        return
    data = await state.get_data()
    draft = data.get('create_draft', {})
    # Валідація
    if not draft.get('name') or not draft.get('url'):
        await call.answer("Заповніть Назву та URL", show_alert=True)
        return
    # Типи
    try:
        draft['expected_status'] = int(draft.get('expected_status') or 200)
        draft['timeout'] = int(draft.get('timeout') or 10)
        draft['check_interval'] = int(draft.get('check_interval') or 60)
    except Exception:
        await call.answer("Некоректні числа у статусі/таймауті/інтервалі", show_alert=True)
        return
    api = await add_api_to_db(draft)
    bot_obj = cast(Bot, call.bot)
    await add_job_to_scheduler(scheduler, bot_obj, api)
    await state.clear()
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, f"✅ API '<b>{api.name}</b>' (ID: {api.id}) створено.", reply_markup=build_api_panel(api, chat_id, call.from_user.id))
    await call.answer("Готово")

@router.callback_query(F.data.startswith("createf:"))
async def cb_create_field(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    if call.from_user.id != settings.ADMIN_USER_ID:
        await call.answer("Тільки адмін", show_alert=True)
        return
    parts = (call.data or '').split(":", 1)
    if len(parts) < 2:
        await call.answer("Невірні дані", show_alert=True)
        return
    field = parts[1]
    await state.update_data(create_field=field)
    hint = {
        'name': "Введіть назву монітора",
        'url': "Введіть повний URL (http/https)",
        'json_keys': "Кома-сепарований список ключів JSON або '-' щоб очистити",
        'headers': "Введіть JSON об'єкт заголовків або '-'",
        'request_body': "Введіть JSON body або '-'",
        'expected_status': "Введіть очікуваний статус (число)",
        'timeout': "Введіть таймаут у секундах (число)",
        'check_interval': "Введіть інтервал у секундах (число)",
    }.get(field, f"Введіть значення для {field}")
    msg = getattr(call, 'message', None)
    if msg is not None:
        await msg.reply(hint)
    else:
        await call.answer(hint, show_alert=True)
    await state.set_state(CreateApiFSM.waiting_for_value)
    await call.answer()

@router.message(AdminFilter(), CreateApiFSM.waiting_for_value)
async def process_create_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data.get('create_field')
    draft = data.get('create_draft', {})
    text = message.text or ''
    try:
        if field in ('expected_status','timeout','check_interval'):
            value = int(text)
        elif field in ('headers','request_body'):
            if text.strip() in ('-','skip','none','null','{}','[]'):
                value = None
            else:
                value = json.loads(text)
        else:
            value = None if text.strip() in ('-','none','null','') else text.strip()
        draft[field] = value
        await state.update_data(create_draft=draft, create_field=None)
        # redraw panel
        try:
            await message.answer("✅ Збережено", reply_markup=build_create_api_panel(draft))
        except Exception:
            await message.answer("✅ Збережено. Поверніться до чернетки через кнопку.")
    except Exception as e:
        await message.answer(f"Помилка: {e}")
    finally:
        # Exit input state but keep draft
        await state.set_state(None)

@router.callback_query(F.data == "sub_all")
async def cb_sub_all(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    chat_id = _get_chat_id_from_call(call)
    if chat_id is None:
        await call.answer("Відкрийте бота у чаті, щоб керувати підписками.", show_alert=True)
        return
    ok = await subscribe_chat(chat_id, None)
    await call.answer("Підписано" if ok else "Вже підписані/помилка", show_alert=False)
    is_sub = await is_chat_subscribed(chat_id, None)
    anom_on = await is_chat_anomaly_notifications_enabled(call.message.chat.id if call.message else getattr(call.from_user, 'id', 0))
    await _safe_edit_reply_markup(call, reply_markup=build_main_menu(is_sub, getattr(call.from_user, 'id', None) == settings.ADMIN_USER_ID, anom_on))

@router.callback_query(F.data == "unsub_all")
async def cb_unsub_all(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    chat_id = _get_chat_id_from_call(call)
    if chat_id is None:
        await call.answer("Відкрийте бота у чаті, щоб керувати підписками.", show_alert=True)
        return
    ok = await unsubscribe_chat(chat_id, None)
    await call.answer("Відписано" if ok else "Не знайдено підписку", show_alert=False)
    is_sub = await is_chat_subscribed(chat_id, None)
    anom_on = await is_chat_anomaly_notifications_enabled(call.message.chat.id if call.message else getattr(call.from_user, 'id', 0))
    await _safe_edit_reply_markup(call, reply_markup=build_main_menu(is_sub, getattr(call.from_user, 'id', None) == settings.ADMIN_USER_ID, anom_on))

@router.callback_query(F.data.startswith("api:"))
async def cb_api(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    try:
        api_id = int((call.data or '').split(":")[1])
    except Exception:
        await call.answer("Невірні дані", show_alert=True)
        return
    api = await get_api_by_id(api_id)
    if not api:
        await call.answer("API не знайдено", show_alert=True)
        return
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer()

@router.callback_query(F.data.startswith("mute:"))
async def cb_mute_menu(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    api_id = int((call.data or '').split(":")[1])
    await _safe_edit_reply_markup(call, reply_markup=build_mute_menu(api_id))
    await call.answer()

@router.callback_query(F.data.startswith("mute_set:"))
async def cb_mute_set(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    _, id_str, dur = (call.data or '').split(":", 2)
    api_id = int(id_str)
    until = None
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if dur == '1h':
        until = now + timedelta(hours=1)
    elif dur == '8h':
        until = now + timedelta(hours=8)
    elif dur == '24h':
        until = now + timedelta(hours=24)
    elif dur == 'forever':
        until = None
    await set_api_mute(api_id, True, until)
    api = await get_api_by_id(api_id)
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("🔕 Заглушено")

@router.callback_query(F.data.startswith("unmute:"))
async def cb_unmute(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    api_id = int((call.data or '').split(":")[1])
    await set_api_mute(api_id, False, None)
    api = await get_api_by_id(api_id)
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("🔔 Увімкнено сповіщення")

@router.callback_query(F.data.startswith("anom:"))
async def cb_anomaly_toggle(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    _, id_str, val = (call.data or '').split(":", 2)
    api_id = int(id_str)
    enabled = bool(int(val))
    await set_anomaly_alerts(api_id, enabled)
    api = await get_api_by_id(api_id)
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("Оновлено")

@router.callback_query(F.data.startswith("anom_cfg:"))
async def cb_anomaly_cfg(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    try:
        api_id = int((call.data or '').split(":")[1])
    except Exception:
        await call.answer("Невірні дані", show_alert=True)
        return
    ok = await _safe_edit_reply_markup(call, reply_markup=build_anom_menu(api_id))
    if not ok:
        await call.answer("Налаштування доступні у чаті з ботом.", show_alert=True)
    await call.answer()

@router.callback_query(F.data.startswith("aset:"))
async def cb_anomaly_set(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    parts = (call.data or '').split(":")
    if len(parts) < 4:
        await call.answer("Невірні дані", show_alert=True)
        return
    api_id = int(parts[1])
    kind = parts[2]
    if kind == 'sens':
        try:
            val = float(parts[3])
        except Exception:
            await call.answer("Невірне значення", show_alert=True)
            return
        await set_anomaly_params(api_id, sensitivity=val)
    elif kind == 'mon' and len(parts) >= 5:
        m = int(parts[3]); n = int(parts[4])
        await set_anomaly_params(api_id, m=m, n=n)
    api = await get_api_by_id(api_id)
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("Збережено")

@router.callback_query(F.data.startswith("edit:"))
async def cb_edit(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    if call.from_user.id != settings.ADMIN_USER_ID:
        await call.answer("Тільки адмін", show_alert=True)
        return
    api_id = int((call.data or '').split(":")[1])
    # Show editable fields
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Name", callback_data=f"editf:{api_id}:name"), InlineKeyboardButton(text="URL", callback_data=f"editf:{api_id}:url")],
        [InlineKeyboardButton(text="Method", callback_data=f"editf:{api_id}:method"), InlineKeyboardButton(text="Expected Status", callback_data=f"editf:{api_id}:expected_status")],
        [InlineKeyboardButton(text="Timeout", callback_data=f"editf:{api_id}:timeout"), InlineKeyboardButton(text="Interval", callback_data=f"editf:{api_id}:check_interval")],
        [InlineKeyboardButton(text="JSON Keys", callback_data=f"editf:{api_id}:json_keys")],
        [InlineKeyboardButton(text="Headers (JSON)", callback_data=f"editf:{api_id}:headers")],
        [InlineKeyboardButton(text="Body (JSON)", callback_data=f"editf:{api_id}:request_body")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"api:{api_id}")]
    ])
    await _safe_edit_reply_markup(call, reply_markup=kb)
    await call.answer()

@router.callback_query(F.data.startswith("editf:"))
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    if not await _guard_callback_access(call):
        return
    if call.from_user.id != settings.ADMIN_USER_ID:
        await call.answer("Тільки адмін", show_alert=True)
        return
    _, id_str, field = (call.data or '').split(":", 2)
    api_id = int(id_str)
    await state.update_data(edit_api_id=api_id, edit_field=field)
    await state.set_state(EditApiFSM.waiting_for_value)
    msg = getattr(call, 'message', None)
    if msg is not None:
        await msg.reply(f"Введіть нове значення для <b>{field}</b>:")
    else:
        await call.answer("Надішліть значення у приватному чаті з ботом.", show_alert=True)
    await call.answer()

@router.message(EditApiFSM.waiting_for_value)
async def process_edit_value(message: Message, state: FSMContext):
    if not await _guard_message_access(message):
        return
    data = await state.get_data()
    api_id_val = data.get('edit_api_id')
    try:
        api_id = int(str(api_id_val))
    except Exception:
        await message.answer("Сеанс редагування втрачено. Спробуйте ще раз.")
        await state.clear()
        return
    field = data.get('edit_field')
    val_raw = message.text or ''
    # Type parsing
    try:
        if field in ('expected_status','timeout','check_interval'):
            value = int(val_raw)
        elif field in ('headers','request_body'):
            if val_raw.strip() in ('-','skip','none','null','{}','[]'):
                value = None
            else:
                value = json.loads(val_raw)
        else:
            value = val_raw
        updated = await update_api_fields(api_id, {field: value})
        if updated:
            await message.answer("✅ Збережено. Оновлено панель.")
            # try to refresh panel if recent panel exists is complex; just confirm.
        else:
            await message.answer("Не знайдено API")
    except Exception as e:
        await message.answer(f"Помилка: {e}")
    finally:
        await state.clear()

@router.callback_query(F.data == "chart_menu")
async def cb_chart_menu(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    ov = await get_chart_overrides()
    rows = [f"<code>{k}</code> = <b>{v}</b>" for k, v in sorted(ov.items())]
    if not rows:
        rows.append("(використовуються значення з .env)")
    text = "<b>Поточні налаштування графіків</b>\n" + "\n".join(rows)
    ok = await _safe_edit_text(call, text, reply_markup=build_chart_kb(ov))
    if not ok:
        await call.answer("Відкрийте бота у чаті, щоб керувати налаштуваннями графіків.", show_alert=True)
    await call.answer()

def build_chart_kb(ov: dict[str, Any]) -> InlineKeyboardMarkup:
    eff: dict[str, Any] = get_effective_chart_config_sync(ov)
    ag = str(eff.get('CHART_AGGREGATION') or 'per_minute').lower()
    ys = str(eff.get('CHART_Y_SCALE') or 'log').lower()

    def as_bool(name: str, default: int) -> bool:
        try:
            return bool(int(str(eff.get(name, default))))
        except Exception:
            return bool(default)

    raw_on = as_bool('CHART_SHOW_RAW_LINE', 1)
    an_on = as_bool('CHART_MARK_ANOMALIES', 1)
    ew_on = as_bool('CHART_SHOW_EWMA', 1)
    ucl_on = as_bool('CHART_SHOW_UCL', 1)

    def mark(txt: str, cond: bool) -> str:
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
        [
            InlineKeyboardButton(text="⬅️ Меню", callback_data="menu"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

@router.message(AdminFilter(), Command("add_full"))
async def cmd_add_full(message: Message, state: FSMContext):
    if not await _guard_message_access(message):
        return
    await state.clear()
    await message.answer(
        "<b>Крок 1/4:</b> Введіть унікальне ім'я для цього API (напр., 'Продакшн API платежів')."
    )
    await state.set_state(AddFullApiFSM.waiting_for_name)

@router.message(AdminFilter(), AddFullApiFSM.waiting_for_name)
async def process_name(message: Message, state: FSMContext):
    if not await _guard_message_access(message):
        return
    await state.update_data(name=message.text)
    await message.answer(
        "<b>Крок 2/4:</b> Надішліть основні дані API у форматі:\n"
        "<code>url [method] [status] [timeout] [interval] [json_keys]</code>\n\n"
        "<i>Приклад:</i> <code>[https://api.example.com](https://api.example.com) POST 201 10 60 status,data</code>"
    )
    await state.set_state(AddFullApiFSM.waiting_for_api_data)

@router.message(AdminFilter(), AddFullApiFSM.waiting_for_api_data)
async def process_full_api_data(message: Message, state: FSMContext):
    if not await _guard_message_access(message):
        return
    try:
        api_data: dict[str, Any] = cast(dict[str, Any], parse_add_command(message.text or ""))
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
    if not await _guard_message_access(message):
        return
    headers = None
    txt = (message.text or '').strip()
    if txt.lower() not in ['-', 'skip']:
        try:
            headers = json.loads(txt)
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
    if not await _guard_message_access(message):
        return
    body = None
    txt = (message.text or '').strip()
    if txt.lower() not in ['-', 'skip']:
        try:
            body = json.loads(txt)
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
    if not await _guard_message_access(message):
        return
    parts = (message.text or '').split()
    if len(parts) < 2:
        await message.answer("Вкажіть ID. Приклад: <code>/stats 1 1h</code> або <code>/stats 1 24h</code>")
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

    stats_data = cast(dict[str, Any], await get_stats_for_period(api_id, period))
    if not stats_data:
        await message.answer(f"Некоректний період: {period}. Доступні: 1h, 6h, 12h, 24h, 7d, 30d.")
        return
    
    history_data = await get_history_for_period(api_id, period)
    chart_overrides = await get_chart_overrides()
    anom_stats = cast(dict[str, Any], await get_anomaly_stats_for_period(api_id, period))
    ml_metric = await get_latest_ml_metric(api_id)
    ucl_hint: Optional[float] = None
    if ml_metric is not None and getattr(ml_metric, 'ucl_ms', None) is not None:
        try:
            ucl_hint = float(getattr(ml_metric, 'ucl_ms'))
        except Exception:
            ucl_hint = None
    avg_rt = float(stats_data.get("avg_response_time_ms", 0) or 0)
    api_name = str(getattr(api, 'name', api_id))
    chart_buffer = await generate_statistics_chart(
        history_data, api_name, period, avg_rt, ucl_hint, chart_overrides
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
    report_caption = format_statistics_report(api_name, stats_data, ml_part, anom_stats)
    
    await message.reply_photo(
        photo=BufferedInputFile(chart_buffer.read(), filename=f"stats_{api_id}_{period}.png"),
        caption=report_caption
    )

@router.message(AdminFilter(), Command("chart"))
async def cmd_chart(message: Message):
    if not await _guard_message_access(message):
        return
    parts = (message.text or '').split()
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
    if not await _guard_callback_access(call):
        return
    try:
        _, key, value = (call.data or '').split(":", 2)
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
    ok = await _safe_edit_text(call, text, reply_markup=build_chart_kb(ov))
    if not ok:
        await call.answer("Відкрийте бота у чаті, щоб керувати налаштуваннями графіків.", show_alert=True)

@router.message(AdminFilter(), Command("list_apis"))
async def cmd_list_apis(message: Message):
    if not await _guard_message_access(message):
        return
    apis = await get_all_apis()
    if not apis:
        await message.answer("Список API для моніторингу порожній.")
        return

    response_text = "<b>API, що моніторяться:</b>\n\n"
    for api in apis:
        status_icon = "🟢" if bool(getattr(api, 'is_up', False)) else "🔴"
        active_icon = "▶️" if bool(getattr(api, 'is_active', False)) else "⏸️"
        response_text += f"{active_icon} {status_icon} <b>{api.name}</b> (ID: <code>{api.id}</code>)\n"
    await message.answer(response_text)

@router.message(AdminFilter(), Command("status"))
async def cmd_status(message: Message):
    if not await _guard_message_access(message):
        return
    try:
        api_id = int((message.text or '').split()[1])
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
        InlineKeyboardButton(text=("⏸️ Пауза" if bool(getattr(api, 'is_active', False)) else "▶️ Відновити"), callback_data=(f"pause:{api.id}" if bool(getattr(api, 'is_active', False)) else f"resume:{api.id}")),
    ],[
        InlineKeyboardButton(text="🔔 Підписатися", callback_data=f"sub:{api.id}"),
        InlineKeyboardButton(text="🔕 Відписатися", callback_data=f"unsub:{api.id}"),
    ]])
    await message.answer(format_api_status(api), reply_markup=kb)

async def _toggle_api(message: Message, scheduler: AsyncIOScheduler, is_active: bool):
    command_name = "resume_api" if is_active else "pause_api"
    try:
        api_id = int((message.text or '').split()[1])
    except (IndexError, ValueError):
        await message.answer(f"Вкажіть ID. Приклад: <code>/{command_name} 1</code>")
        return

    api = await toggle_api_monitoring(api_id, is_active)
    if not api:
        await message.answer(f"API з ID {api_id} не знайдено.")
        return

    if is_active:
        await add_job_to_scheduler(scheduler, cast(Bot, message.bot), api)
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
        api_id = int((message.text or '').split()[1])
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
    if not await _guard_message_access(message):
        return
    parts = (message.text or '').split()
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
    if not await _guard_message_access(message):
        return
    parts = (message.text or '').split()
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
    if not await _guard_message_access(message):
        return
    parts = (message.text or '').split()
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
    if not await _guard_message_access(message):
        return
    parts = (message.text or '').split()
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
    if not await _guard_callback_access(call):
        return
    try:
        _, id_str, period = (call.data or '').split(':')
        api_id = int(id_str)
    except Exception:
        await call.answer("Невірні дані", show_alert=True)
        return
    api = await get_api_by_id(api_id)
    if not api:
        await call.answer("API не знайдено", show_alert=True)
        return
    stats_data = cast(dict[str, Any], await get_stats_for_period(api_id, period))
    history_data = await get_history_for_period(api_id, period)
    ml_metric = await get_latest_ml_metric(api_id)
    anom_stats = cast(dict[str, Any], await get_anomaly_stats_for_period(api_id, period))
    chart_overrides = await get_chart_overrides()
    ucl_hint: Optional[float] = None
    if ml_metric is not None and getattr(ml_metric, 'ucl_ms', None) is not None:
        try:
            ucl_hint = float(getattr(ml_metric, 'ucl_ms'))
        except Exception:
            ucl_hint = None
    avg_rt = float(stats_data.get("avg_response_time_ms", 0) or 0)
    api_name = str(getattr(api, 'name', api_id))
    chart_buffer = await generate_statistics_chart(
        history_data, api_name, period, avg_rt, ucl_hint, chart_overrides
    )
    ml_part = None
    if ml_metric:
        ml_part = {"median_ms": ml_metric.median_ms, "mad_ms": ml_metric.mad_ms, "ewma_ms": ml_metric.ewma_ms, "ucl_ms": ml_metric.ucl_ms, "window": ml_metric.window_size}
    caption = format_statistics_report(api_name, stats_data, ml_part, anom_stats)
    msg = getattr(call, 'message', None)
    if msg is not None:
        await msg.reply_photo(photo=BufferedInputFile(chart_buffer.read(), filename=f"stats_{api_id}_{period}.png"), caption=caption)
    else:
        await call.answer("Графік доступний у чаті з ботом.", show_alert=True)
    await call.answer()

@router.callback_query(F.data.startswith("pause:"))
async def cb_pause(call: CallbackQuery, scheduler: AsyncIOScheduler):
    if not await _guard_callback_access(call):
        return
    api_id = int((call.data or '').split(":")[1])
    await toggle_api_monitoring(api_id, False)
    remove_job_from_scheduler(scheduler, api_id)
    api = await get_api_by_id(api_id)
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("Поставлено на паузу")

@router.callback_query(F.data.startswith("resume:"))
async def cb_resume(call: CallbackQuery, scheduler: AsyncIOScheduler):
    if not await _guard_callback_access(call):
        return
    api_id = int((call.data or '').split(":")[1])
    api = await toggle_api_monitoring(api_id, True)
    if api:
        bot_obj = cast(Bot, call.bot)
        await add_job_to_scheduler(scheduler, bot_obj, api)
    api = await get_api_by_id(api_id)
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("Відновлено")

@router.callback_query(F.data.startswith("sub:"))
async def cb_sub(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    api_id = int((call.data or '').split(":")[1])
    chat_id = _get_chat_id_from_call(call)
    if chat_id is None:
        await call.answer("Відкрийте бота у чаті, щоб оформити підписку.", show_alert=True)
        return
    await subscribe_chat(chat_id, api_id)
    api = await get_api_by_id(api_id)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("Підписано")

@router.callback_query(F.data.startswith("unsub:"))
async def cb_unsub(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    api_id = int((call.data or '').split(":")[1])
    chat_id = _get_chat_id_from_call(call)
    if chat_id is None:
        await call.answer("Відкрийте бота у чаті, щоб змінити підписку.", show_alert=True)
        return
    await unsubscribe_chat(chat_id, api_id)
    api = await get_api_by_id(api_id)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    await call.answer("Відписано")

@router.callback_query(F.data.startswith("del:"))
async def cb_delete(call: CallbackQuery, scheduler: AsyncIOScheduler):
    if not await _guard_callback_access(call):
        return
    if call.from_user.id != settings.ADMIN_USER_ID:
        await call.answer("Тільки адмін", show_alert=True)
        return
    api_id = int((call.data or '').split(":")[1])
    # Stop job and delete
    remove_job_from_scheduler(scheduler, api_id)
    deleted = await delete_api_from_db(api_id)
    if deleted:
        await call.answer("Видалено")
        await cb_list_apis(call)
    else:
        await call.answer("Не вдалося", show_alert=True)

@router.callback_query(F.data.startswith("check:"))
async def cb_check_now(call: CallbackQuery):
    if not await _guard_callback_access(call):
        return
    try:
        api_id = int((call.data or '').split(":")[1])
    except Exception:
        await call.answer("Невірні дані", show_alert=True)
        return
    await call.answer("Перевіряю…")
    try:
        await check_api(cast(Bot, call.bot), api_id)
    except Exception:
        pass
    api = await get_api_by_id(api_id)
    chat_id = _get_chat_id_from_call(call)
    await _safe_edit_text(call, _format_api_row(api), reply_markup=build_api_panel(api, chat_id, getattr(call.from_user, 'id', None)))
    # короткий тост
    try:
        await call.answer("Готово")
    except Exception:
        pass

@router.message(StateFilter(None))
async def unknown_command(message: Message):
    # Ignore random messages in group chats
    if getattr(message.chat, 'type', 'private') != 'private':
        return
    from database import is_chat_subscribed
    is_sub = await is_chat_subscribed(message.chat.id, None)
    is_admin = getattr(message.from_user, 'id', None) == settings.ADMIN_USER_ID
    anom_on = await is_chat_anomaly_notifications_enabled(message.chat.id)
    await message.answer("Невідома команда. Використайте меню нижче.", reply_markup=build_main_menu(is_sub, is_admin, anom_on))