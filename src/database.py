# src/database.py
# Файл для роботи з базою даних (SQLite або PostgreSQL)

import datetime
import logging
from typing import AsyncGenerator, List, Optional, Tuple

from sqlalchemy import (Column, Integer, String, DateTime, Boolean, JSON,
                        create_engine, select, func, ForeignKey)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

from config import settings
from utils import parse_period_to_timedelta

logger = logging.getLogger(__name__)

DB_URL = settings.DATABASE_URL or "sqlite+aiosqlite:///./db.sqlite3"
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

class Incident(Base):
    __tablename__ = "incidents"
    id = Column(Integer, primary_key=True)
    api_id = Column(Integer, ForeignKey("monitored_apis.id", ondelete="CASCADE"), nullable=False)
    start_time = Column(DateTime, nullable=False, index=True)
    end_time = Column(DateTime, nullable=True, index=True)

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