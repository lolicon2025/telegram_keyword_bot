from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List, Any

import regex as re  # better unicode handling than built-in re
from loguru import logger


@dataclass
class RuleDTO:
    id: int
    match_type: str
    pattern: str
    reply: str
    priority: int
    enabled: bool
    compiled: Any | None = None  # compiled regex or other artifacts


class RuleCache:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._cache: Dict[int, Tuple[float, List[RuleDTO]]] = {}  # group_id -> (loaded_ts, rules)

    def invalidate(self, group_id: int) -> None:
        self._cache.pop(group_id, None)

    def get_if_fresh(self, group_id: int) -> Optional[List[RuleDTO]]:
        item = self._cache.get(group_id)
        if not item:
            return None
        ts, rules = item
        if time.time() - ts > self.ttl:
            self._cache.pop(group_id, None)
            return None
        return rules

    def set(self, group_id: int, rules: List[RuleDTO]) -> None:
        # precompile regex patterns for speed and to catch invalid ones early
        for r in rules:
            if r.match_type == "regex":
                try:
                    r.compiled = re.compile(r.pattern, flags=re.IGNORECASE)
                except Exception as e:
                    logger.warning(f"Invalid regex for rule {r.id}: {e}. Skipping compilation; rule will be ignored.")
                    r.compiled = None
        self._cache[group_id] = (time.time(), rules)
