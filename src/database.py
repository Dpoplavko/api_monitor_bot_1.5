# src/database.py
# Файл для роботи з базою даних (SQLite або PostgreSQL)

import datetime
import logging
from typing import AsyncGenerator, List, Optional, Tuple, Dict

from sqlalchemy import (Column, Integer, String, DateTime, Boolean, JSON,
                        create_engine, select, func, ForeignKey, Index)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

from config import settings
from utils import parse_period_to_timedelta

logger = logging.getLogger(__name__)

DB_URL = settings.DATABASE_URL or "sqlite+aiosqlite:///./data/db.sqlite3"
IS_POSTGRES = DB_URL.startswith("postgresql")

engine_args = {"connect_args": {"check_same_thread": False}} if not IS_POSTGRES else {}
async_engine = create_async_engine(DB_URL, echo=False, **engine_args)

AsyncSessionFactory = sessionmaker(
    bind=async_engine, class_=AsyncSession, expire_on_commit=False
)

Base = declarative_base()

class MonitoredAPI(Base):
    __tablename__ = "monitored_apis"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, default="Unnamed API")
    url = Column(String, nullable=False)
    method = Column(String, default="GET")
    headers = Column(JSON, nullable=True)
    request_body = Column(JSON, nullable=True)
    expected_status = Column(Integer, default=200)
    timeout = Column(Integer, default=10)
    check_interval = Column(Integer, default=60)
    json_keys = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    is_up = Column(Boolean, default=True)
    last_checked = Column(DateTime, nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_response_time = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)
    consecutive_failures = Column(Integer, default=0)
    consecutive_successes = Column(Integer, default=0)
    incident_start_time = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class CheckHistory(Base):
    __tablename__ = "check_history"
    id = Column(Integer, primary_key=True)
    api_id = Column(Integer, ForeignKey("monitored_apis.id", ondelete="CASCADE"), nullable=False)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    is_ok = Column(Boolean, nullable=False)
    response_time_ms = Column(Integer, nullable=True)
    status_code = Column(Integer, nullable=True)
    __table_args__ = (
        Index('ix_check_history_api_id_timestamp', 'api_id', 'timestamp'),
    )

class Incident(Base):
    __tablename__ = "incidents"
    id = Column(Integer, primary_key=True)
    api_id = Column(Integer, ForeignKey("monitored_apis.id", ondelete="CASCADE"), nullable=False)
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=True, index=True)

class MLMetric(Base):
    __tablename__ = "ml_metrics"
    id = Column(Integer, primary_key=True)
    api_id = Column(Integer, ForeignKey("monitored_apis.id", ondelete="CASCADE"), nullable=False, index=True)
    computed_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    window_size = Column(Integer, default=200)
    median_ms = Column(Integer, nullable=True)
    mad_ms = Column(Integer, nullable=True)
    ewma_ms = Column(Integer, nullable=True)
    ucl_ms = Column(Integer, nullable=True)  # Upper Control Limit

class AnomalyEvent(Base):
    __tablename__ = "anomaly_events"
    id = Column(Integer, primary_key=True)
    api_id = Column(Integer, ForeignKey("monitored_apis.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow, index=True)
    response_time_ms = Column(Integer, nullable=False)
    score = Column(Integer, nullable=True)
    reason = Column(String, nullable=True)

class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    chat_id = Column(Integer, index=True, nullable=False)
    api_id = Column(Integer, ForeignKey("monitored_apis.id", ondelete="CASCADE"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    __table_args__ = (
        Index('ux_subscription_chat_api', 'chat_id', 'api_id', unique=True),
    )

class NotificationState(Base):
    __tablename__ = "notification_state"
    id = Column(Integer, primary_key=True)
    api_id = Column(Integer, ForeignKey("monitored_apis.id", ondelete="CASCADE"), nullable=False, index=True)
    last_down_reminder_at = Column(DateTime, nullable=True)

class AppConfig(Base):
    __tablename__ = "app_config"
    key = Column(String, primary_key=True)
    value = Column(String, nullable=True)

async def init_db():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def add_api_to_db(data: dict) -> MonitoredAPI:
    async with AsyncSessionFactory() as session:
        api = MonitoredAPI(**data)
        session.add(api)
        await session.commit()
        await session.refresh(api)
        return api

async def get_all_apis() -> List[MonitoredAPI]:
    async with AsyncSessionFactory() as session:
        return (await session.execute(select(MonitoredAPI))).scalars().all()

async def get_all_active_apis() -> List[MonitoredAPI]:
    async with AsyncSessionFactory() as session:
        return (await session.execute(select(MonitoredAPI).where(MonitoredAPI.is_active == True))).scalars().all()

async def get_api_by_id(api_id: int) -> Optional[MonitoredAPI]:
    async with AsyncSessionFactory() as session:
        return (await session.execute(select(MonitoredAPI).where(MonitoredAPI.id == api_id))).scalars().first()

async def update_api_status(api_id: int, update_data: dict):
    async with AsyncSessionFactory() as session:
        api = await session.get(MonitoredAPI, api_id)
        if api:
            for key, value in update_data.items():
                setattr(api, key, value)
            await session.commit()

async def toggle_api_monitoring(api_id: int, is_active: bool) -> Optional[MonitoredAPI]:
    async with AsyncSessionFactory() as session:
        api = await session.get(MonitoredAPI, api_id)
        if api:
            api.is_active = is_active
            await session.commit()
            await session.refresh(api)
            return api
        return None

async def delete_api_from_db(api_id: int) -> bool:
    async with AsyncSessionFactory() as session:
        api = await session.get(MonitoredAPI, api_id)
        if api:
            await session.delete(api)
            await session.commit()
            return True
        return False

async def log_check_to_history(api_id: int, is_ok: bool, response_time_ms: int, status_code: Optional[int]):
    async with AsyncSessionFactory() as session:
        history_entry = CheckHistory(api_id=api_id, is_ok=is_ok, response_time_ms=response_time_ms, status_code=status_code)
        session.add(history_entry)
        await session.commit()

async def create_incident(api_id: int, start_time: datetime.datetime) -> Incident:
    async with AsyncSessionFactory() as session:
        incident = Incident(api_id=api_id, start_time=start_time)
        session.add(incident)
        await session.commit()
        await session.refresh(incident)
        return incident

async def end_incident(api_id: int, start_time: datetime.datetime, end_time: datetime.datetime):
    async with AsyncSessionFactory() as session:
        stmt = select(Incident).where(
            Incident.api_id == api_id,
            Incident.start_time == start_time,
            Incident.end_time.is_(None)
        ).order_by(Incident.start_time.desc())
        incident = (await session.execute(stmt)).scalars().first()
        if incident:
            incident.end_time = end_time
            await session.commit()

async def get_history_for_period(api_id: int, period: str) -> List[CheckHistory]:
    start_date = parse_period_to_timedelta(period)
    if not start_date:
        return []
    async with AsyncSessionFactory() as session:
        stmt = select(CheckHistory).where(
            CheckHistory.api_id == api_id,
            CheckHistory.timestamp >= start_date
        ).order_by(CheckHistory.timestamp.asc())
        return (await session.execute(stmt)).scalars().all()

async def get_stats_for_period(api_id: int, period: str) -> dict:
    start_date = parse_period_to_timedelta(period)
    if not start_date: return {}

    async with AsyncSessionFactory() as session:
        history_stmt = select(
            func.count(),
            func.sum(func.cast(CheckHistory.is_ok, Integer)),
            func.avg(CheckHistory.response_time_ms)
        ).where(CheckHistory.api_id == api_id, CheckHistory.timestamp >= start_date)
        total_checks, ok_checks, avg_response_time = (await session.execute(history_stmt)).first() or (0, 0, 0)
        
        uptime = (ok_checks / total_checks * 100) if total_checks and ok_checks is not None else 100

        incidents_stmt = select(Incident).where(
            Incident.api_id == api_id,
            Incident.start_time >= start_date
        )
        incidents = (await session.execute(incidents_stmt)).scalars().all()

        incident_count = len(incidents)
        total_downtime = datetime.timedelta()
        for inc in incidents:
            end_time = inc.end_time or datetime.datetime.utcnow()
            total_downtime += end_time - inc.start_time
        
        avg_downtime = total_downtime / incident_count if incident_count > 0 else datetime.timedelta()

    return {
        "period": period, "uptime_percent": uptime, "avg_response_time_ms": avg_response_time,
        "incident_count": incident_count, "total_downtime": total_downtime, "avg_downtime": avg_downtime,
    }

# --- ML helpers ---
async def get_recent_history_points(api_id: int, limit: int = 200) -> List[CheckHistory]:
    async with AsyncSessionFactory() as session:
        stmt = select(CheckHistory).where(CheckHistory.api_id == api_id).order_by(CheckHistory.timestamp.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
        rows.reverse()
        return rows

async def save_ml_metric(api_id: int, metrics: dict) -> MLMetric:
    async with AsyncSessionFactory() as session:
        rec = MLMetric(api_id=api_id, **metrics)
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec

async def get_latest_ml_metric(api_id: int) -> Optional[MLMetric]:
    async with AsyncSessionFactory() as session:
        stmt = select(MLMetric).where(MLMetric.api_id == api_id).order_by(MLMetric.computed_at.desc()).limit(1)
        return (await session.execute(stmt)).scalars().first()

async def log_anomaly_event(api_id: int, response_time_ms: int, score: float, reason: str):
    async with AsyncSessionFactory() as session:
        rec = AnomalyEvent(api_id=api_id, response_time_ms=response_time_ms, score=int(score), reason=reason)
        session.add(rec)
        await session.commit()

async def get_last_anomaly_time(api_id: int) -> Optional[datetime.datetime]:
    async with AsyncSessionFactory() as session:
        stmt = select(AnomalyEvent).where(AnomalyEvent.api_id == api_id).order_by(AnomalyEvent.timestamp.desc()).limit(1)
        last = (await session.execute(stmt)).scalars().first()
        return last.timestamp if last else None

async def subscribe_chat(chat_id: int, api_id: Optional[int] = None) -> bool:
    async with AsyncSessionFactory() as session:
        # prevent duplicates via unique index; ignore if exists
        try:
            sub = Subscription(chat_id=chat_id, api_id=api_id)
            session.add(sub)
            await session.commit()
            return True
        except Exception:
            await session.rollback()
            return False

async def unsubscribe_chat(chat_id: int, api_id: Optional[int] = None) -> bool:
    async with AsyncSessionFactory() as session:
        stmt = select(Subscription).where(Subscription.chat_id == chat_id)
        if api_id is None:
            stmt = stmt.where(Subscription.api_id.is_(None))
        else:
            stmt = stmt.where(Subscription.api_id == api_id)
        rec = (await session.execute(stmt)).scalars().first()
        if rec:
            await session.delete(rec)
            await session.commit()
            return True
        return False

async def get_subscribers_for_api(api_id: int) -> List[int]:
    async with AsyncSessionFactory() as session:
        global_stmt = select(Subscription.chat_id).where(Subscription.api_id.is_(None))
        api_stmt = select(Subscription.chat_id).where(Subscription.api_id == api_id)
        globals_list = [row[0] for row in (await session.execute(global_stmt)).all()]
        api_list = [row[0] for row in (await session.execute(api_stmt)).all()]
        # Always include admin
        result = set(globals_list + api_list + [int(settings.ADMIN_USER_ID)])
        return list(result)

async def get_or_create_notification_state(api_id: int) -> NotificationState:
    async with AsyncSessionFactory() as session:
        stmt = select(NotificationState).where(NotificationState.api_id == api_id)
        rec = (await session.execute(stmt)).scalars().first()
        if not rec:
            rec = NotificationState(api_id=api_id)
            session.add(rec)
            await session.commit()
            await session.refresh(rec)
        return rec

async def update_down_reminder_time(api_id: int, ts: datetime.datetime):
    async with AsyncSessionFactory() as session:
        stmt = select(NotificationState).where(NotificationState.api_id == api_id)
        rec = (await session.execute(stmt)).scalars().first()
        if not rec:
            rec = NotificationState(api_id=api_id, last_down_reminder_at=ts)
            session.add(rec)
        else:
            rec.last_down_reminder_at = ts
        await session.commit()

# --- Runtime App Config ---
async def get_config_value(key: str) -> Optional[str]:
    async with AsyncSessionFactory() as session:
        rec = (await session.execute(select(AppConfig).where(AppConfig.key == key))).scalars().first()
        return rec.value if rec else None

async def set_config_value(key: str, value: Optional[str]) -> None:
    async with AsyncSessionFactory() as session:
        rec = (await session.execute(select(AppConfig).where(AppConfig.key == key))).scalars().first()
        if rec:
            rec.value = value
        else:
            rec = AppConfig(key=key, value=value)
            session.add(rec)
        await session.commit()

async def get_all_config() -> Dict[str, str]:
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(select(AppConfig))).scalars().all()
        return {r.key: r.value for r in rows}