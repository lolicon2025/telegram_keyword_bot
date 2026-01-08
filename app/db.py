from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


@dataclass
class Database:
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]

    @classmethod
    def from_url(cls, url: str) -> "Database":
        engine = create_async_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            echo=False,
        )
        sm = async_sessionmaker(engine, expire_on_commit=False)
        return cls(engine=engine, sessionmaker=sm)

    def session(self) -> AsyncSession:
        return self.sessionmaker()

    async def dispose(self) -> None:
        await self.engine.dispose()
