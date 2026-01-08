from __future__ import annotations

from typing import Optional
import time

from rapidfuzz import fuzz
from loguru import logger

from app.cache import RuleDTO


class Throttle:
    def __init__(self, cooldown_seconds: int):
        self.cooldown = cooldown_seconds
        self._last: dict[tuple[int, int], float] = {}  # (group_id, rule_id) -> last_ts

    def allow(self, group_id: int, rule_id: int) -> bool:
        now = time.time()
        k = (group_id, rule_id)
        last = self._last.get(k, 0.0)
        if now - last < self.cooldown:
            return False
        self._last[k] = now
        return True


def match_rule(text: str, rule: RuleDTO) -> bool:
    if not rule.enabled:
        return False
    t = text or ""
    p = rule.pattern or ""

    if rule.match_type == "exact":
        return t.strip() == p.strip()

    if rule.match_type == "contains":
        return p in t

    if rule.match_type == "regex":
        if rule.compiled is None:
            return False
        try:
            return rule.compiled.search(t) is not None
        except Exception as e:
            logger.warning(f"Regex match error for rule {rule.id}: {e}")
            return False

    if rule.match_type == "fuzzy":
        # simple fuzzy match: ratio >= 85
        return fuzz.partial_ratio(p, t) >= 85

    return False
