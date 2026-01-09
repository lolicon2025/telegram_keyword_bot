from __future__ import annotations

import asyncio
from app.config import get_settings
from app.db import Database
from app.models import Base  # noqa: F401 (import registers models)


async def main() -> None:
    settings = get_settings()
    db = Database.from_url(settings.database_url)

    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await db.dispose()
    print("OK: tables created.")


if __name__ == "__main__":
    asyncio.run(main())
