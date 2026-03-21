"""
V9 DCA Engine  (v9.4)
======================
v9.4: hedge_engine 제거 → hedge_mode 제외 로직 삭제
      모든 포지션에 일반 DCA 동일 적용
"""
from typing import List, Dict

from v9.types import Intent, MarketSnapshot


def generate_dca_intents(
    snapshot: MarketSnapshot,
    st: Dict,
    cooldowns: Dict,
) -> List[Intent]:
    """DCA Intent 생성 — 모든 포지션 대상 (v9.4)."""
    from v9.strategy.planners import plan_dca
    return plan_dca(snapshot, st, cooldowns)
