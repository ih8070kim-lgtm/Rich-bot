"""
V9 Risk — Slot Manager  (v9.1)
===============================
v9.0 → v9.1 변경:
  1) RISK_SLOTS 가중치: step≥1 포지션  0 → 0.5  (SOFT)
  2) 동적 슬롯 개방: 방향당 초기 2 → T3 도달 시 3 → T4(또는 T3×2) 시 4
  3) can_open_side()에 st 파라미터 추가 (동적 한도 계산)
"""
from typing import Dict, Tuple

from v9.types import SlotCounts
from v9.config import (
    TOTAL_MAX_SLOTS,
    MAX_LONG,
    MAX_SHORT,
    DYNAMIC_SLOT_INITIAL,
    DYNAMIC_SLOT_EXPAND_3_TRIGGER,
    DYNAMIC_SLOT_EXPAND_4_TRIGGER,
    DYNAMIC_SLOT_EXPAND_4_ALT,
)
from v9.execution.position_book import iter_positions, get_pending_entry, is_active


# ─────────────────────────────────────────────────────────────────
# count_slots
# ─────────────────────────────────────────────────────────────────
def count_slots(st: Dict) -> SlotCounts:
    """
    슬롯 카운트. (v10.0 — hedge mode 지원)
    심볼당 Long/Short 각각 독립 카운트.
    """
    hard_total = 0
    hard_long  = 0
    hard_short = 0
    risk_total_f = 0.0
    risk_long_f  = 0.0
    risk_short_f = 0.0

    for sym, sd in st.items():
        if not isinstance(sd, dict):
            continue
        if sd.get("pending_exit"):
            continue

        # ── 활성 포지션 (p_long / p_short) ──────────────────────
        for side, p in iter_positions(sd):
            step = int((p or {}).get("step", 0) or 0)
            if step >= 1:
                continue   # 트레일링 → HARD/RISK 모두 제외
            hard_total += 1
            if side == "buy":  hard_long  += 1
            else:              hard_short += 1
            risk_total_f += 1.0
            if side == "buy":  risk_long_f  += 1.0
            else:              risk_short_f += 1.0

        # ── pending_entry ────────────────────────────────────────
        for pe_side in ("buy", "sell"):
            pe = get_pending_entry(sd, pe_side)
            if not pe:
                continue
            hard_total += 1
            risk_total_f += 1.0
            if pe_side == "buy":
                hard_long    += 1
                risk_long_f  += 1.0
            else:
                hard_short   += 1
                risk_short_f += 1.0

    import math
    risk_total = math.ceil(risk_total_f - 0.0001) if risk_total_f > 0 else 0
    risk_long  = math.ceil(risk_long_f  - 0.0001) if risk_long_f  > 0 else 0
    risk_short = math.ceil(risk_short_f - 0.0001) if risk_short_f > 0 else 0

    return SlotCounts(
        total=hard_total,
        long=hard_long,
        short=hard_short,
        risk_total=risk_total,
        risk_long=risk_long,
        risk_short=risk_short,
    )


# ─────────────────────────────────────────────────────────────────
# 동적 슬롯 한도
# ─────────────────────────────────────────────────────────────────
def get_dynamic_max_per_side(st: Dict, side: str) -> int:
    """
    해당 방향(buy/sell)의 동적 슬롯 한도를 계산.
      - 기본: DYNAMIC_SLOT_INITIAL (2)
      - 어떤 포지션이 dca_level ≥ 2 (T2)  →  3슬롯
      - 어떤 포지션이 dca_level ≥ 3 (T3)  또는  T2 포지션이 2개 이상  →  MAX (4슬롯)
    """
    max_dca_level = 0
    t3_plus_count = 0

    for sym, sd in st.items():
        if not isinstance(sd, dict):
            continue
        for pos_side, p in iter_positions(sd):
            if pos_side != ("buy" if side == "buy" else "sell"):
                continue
            if p.get("hedge_mode"):
                continue
            # ★ v10.1: Pullback은 ASYM 동적 슬롯 트리거 제외
            if p.get("entry_type") == "PULLBACK":
                continue
            dca = int(p.get("dca_level", 1) or 1)
            if dca > max_dca_level:
                max_dca_level = dca
            if dca >= DYNAMIC_SLOT_EXPAND_3_TRIGGER:
                t3_plus_count += 1

    hard_max = MAX_LONG if side == "buy" else MAX_SHORT

    if max_dca_level >= DYNAMIC_SLOT_EXPAND_4_TRIGGER:
        return hard_max                                       # T4 존재 → 4슬롯
    if t3_plus_count >= DYNAMIC_SLOT_EXPAND_4_ALT:
        return hard_max                                       # T3+ 2개 이상 → 4슬롯
    if max_dca_level >= DYNAMIC_SLOT_EXPAND_3_TRIGGER:
        return min(3, hard_max)                               # T3 존재 → 3슬롯
    return DYNAMIC_SLOT_INITIAL                               # 기본 → 2슬롯


# ─────────────────────────────────────────────────────────────────
# 진입 가능 여부
# ─────────────────────────────────────────────────────────────────
def can_open_side(
    slots: SlotCounts,
    side: str,
    st: Dict = None,
    skew_bonus: int = 0,
) -> Tuple[bool, str]:
    """
    신규 진입 가능 여부 (RISK_SLOTS 기준, 동적 한도 적용).
    skew_bonus: SKEW_RELIEF 발동 시 dynamic_max에 가산 (+1 or +2, hard_max 클램프)
    """
    if slots.risk_total >= TOTAL_MAX_SLOTS:
        return False, "REJECT_SLOT_LIMIT_TOTAL"

    hard_max = MAX_LONG if side == "buy" else MAX_SHORT
    if st is not None:
        dyn_max = min(get_dynamic_max_per_side(st, side) + skew_bonus, hard_max)
    else:
        dyn_max = hard_max

    if side == "buy" and slots.risk_long >= dyn_max:
        return False, f"REJECT_LONG_SLOT_LIMIT(dyn={dyn_max},skew+{skew_bonus})"
    if side == "sell" and slots.risk_short >= dyn_max:
        return False, f"REJECT_SHORT_SLOT_LIMIT(dyn={dyn_max},skew+{skew_bonus})"
    return True, "APPROVED"


def can_open_hard(slots: SlotCounts, side: str) -> Tuple[bool, str]:
    """하드캡 기준 진입 가능 여부."""
    if slots.total >= TOTAL_MAX_SLOTS:
        return False, "REJECT_SLOT_LIMIT_TOTAL"
    if side == "buy" and slots.long >= MAX_LONG:
        return False, "REJECT_LONG_SLOT_LIMIT"
    if side == "sell" and slots.short >= MAX_SHORT:
        return False, "REJECT_SHORT_SLOT_LIMIT"
    return True, "APPROVED"
