from __future__ import annotations

from typing import Sequence
from sqlalchemy import select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GroupConfig, Rule, AuditLog


async def ensure_group(session: AsyncSession, group_id: int, title: str | None = None) -> GroupConfig:
    res = await session.execute(select(GroupConfig).where(GroupConfig.group_id == group_id))
    g = res.scalar_one_or_none()
    if g is None:
        g = GroupConfig(group_id=group_id, title=title, enabled=True)
        session.add(g)
        await session.flush()
    else:
        # keep title reasonably fresh
        if title and g.title != title:
            g.title = title
    return g


async def list_rules(session: AsyncSession, group_id: int, limit: int = 30, offset: int = 0) -> Sequence[Rule]:
    res = await session.execute(
        select(Rule)
        .where(Rule.group_id == group_id)
        .order_by(Rule.enabled.desc(), Rule.priority.asc(), Rule.id.asc())
        .limit(limit)
        .offset(offset)
    )
    return res.scalars().all()


async def list_enabled_rules(session: AsyncSession, group_id: int) -> Sequence[Rule]:
    res = await session.execute(
        select(Rule)
        .where(Rule.group_id == group_id, Rule.enabled.is_(True))
        .order_by(Rule.priority.asc(), Rule.id.asc())
    )
    return res.scalars().all()


async def create_rule(
    session: AsyncSession,
    group_id: int,
    match_type: str,
    pattern: str,
    reply: str,
    created_by: int,
    priority: int = 100,
    enabled: bool = True,
) -> Rule:
    rule = Rule(
        group_id=group_id,
        match_type=match_type,
        pattern=pattern,
        reply=reply,
        created_by=created_by,
        priority=priority,
        enabled=enabled,
    )
    session.add(rule)
    await session.flush()
    return rule


async def delete_rule_by_id(session: AsyncSession, group_id: int, rule_id: int) -> bool:
    res = await session.execute(
        delete(Rule).where(Rule.group_id == group_id, Rule.id == rule_id)
    )
    return res.rowcount > 0


async def add_audit(
    session: AsyncSession,
    group_id: int,
    actor_user_id: int,
    action: str,
    before_json: dict | None,
    after_json: dict | None,
) -> None:
    session.add(
        AuditLog(
            group_id=group_id,
            actor_user_id=actor_user_id,
            action=action,
            before_json=before_json,
            after_json=after_json,
        )
    )
