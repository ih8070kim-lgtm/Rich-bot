"""
V9 Risk — Directional Exposure Calculator  (v9.8 신규)
=======================================================
방향별 총 명목 노출(notional exposure)을 계산하고
캡 초과 여부를 판단.

용어 정리:
  notional  = 포지션 수량 × 현재가격  (레버리지 적용 전 명목 금액)
  exposure  = notional  (증거금이 아닌 실제 포지션 크기)
  
  equity = real_balance_usdt (실제 잔고)
  
  cap 기준: equity × EXPOSURE_CAP_DIR / TOTAL
"""
from __future__ import annotations

from typing import Dict, Tuple

from v9.execution.position_book import iter_positions as _iter_pos


def calc_directional_exposure(
    st: Dict,
    prices: Dict[str, float],
) -> Tuple[float, float]:
    """
    현재 포지션 기준 방향별 총 명목 노출 계산.

    Returns:
        (long_notional, short_notional)  — USDT 단위
    """
    long_notional  = 0.0
    short_notional = 0.0

    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        px = float(prices.get(sym, 0.0) or 0.0)
        if px <= 0:
            continue
        for side, p in _iter_pos(sym_st):
            amt = float(p.get("amt", 0.0) or 0.0)
            if amt <= 0:
                continue
            notional = amt * px
            if side == "buy":
                long_notional  += notional
            else:
                short_notional += notional

    return long_notional, short_notional


def check_exposure_cap(
    side: str,
    add_notional: float,
    long_notional: float,
    short_notional: float,
    equity: float,
    cap_dir: float,
    cap_total: float,
) -> Tuple[bool, str]:
    """
    신규 포지션 추가 후 캡 초과 여부 판단.

    Args:
        side:            "buy" or "sell"
        add_notional:    신규 추가될 명목금액
        long_notional:   현재 Long 합산
        short_notional:  현재 Short 합산
        equity:          실제 잔고
        cap_dir:         방향별 상한 배수
        cap_total:       양방향 합산 상한 배수

    Returns:
        (ok: bool, reason: str)
    """
    if equity <= 0:
        return True, ""   # 잔고 미확인 → 통과 (보수적 처리 불가)

    if side == "buy":
        proj_long  = long_notional  + add_notional
        proj_short = short_notional
    else:
        proj_long  = long_notional
        proj_short = short_notional + add_notional

    proj_total = proj_long + proj_short
    dir_notional = proj_long if side == "buy" else proj_short

    dir_limit   = equity * cap_dir
    total_limit = equity * cap_total

    if dir_notional > dir_limit:
        return False, (
            f"DIR_CAP_{side.upper()}_BREACH "
            f"{dir_notional:.0f}>{dir_limit:.0f} "
            f"({dir_notional/equity:.2f}x>{cap_dir}x equity)"
        )
    if proj_total > total_limit:
        return False, (
            f"TOTAL_CAP_BREACH "
            f"{proj_total:.0f}>{total_limit:.0f} "
            f"({proj_total/equity:.2f}x>{cap_total}x equity)"
        )

    return True, ""


def check_asym_cover_ratio(
    asym_side: str,
    add_notional: float,
    long_notional: float,
    short_notional: float,
    cover_ratio_min: float,
    cover_ratio_max: float,
) -> Tuple[bool, str]:
    """
    ASYM_FORCE 진입 시 커버 비율 검사.

    커버 비율 = 반대방향(ASYM) 노출 / 손실방향 노출
      - 너무 낮으면 (< min): 커버 효과가 약함 → 진입 허용 (커버 강화 목적)
      - 너무 높으면 (> max): 독립 베팅 수준 → 진입 거부

    Args:
        asym_side:      ASYM이 추가될 방향 ("buy"/"sell")
        add_notional:   ASYM 추가 명목금액
        long_notional:  현재 Long 합산
        short_notional: 현재 Short 합산

    Returns:
        (ok: bool, reason: str)
    """
    # 손실방향 = ASYM 반대방향
    if asym_side == "buy":
        loss_notional  = short_notional   # Short이 물린 상황에서 Long ASYM
        cover_notional = long_notional + add_notional
    else:
        loss_notional  = long_notional    # Long이 물린 상황에서 Short ASYM
        cover_notional = short_notional + add_notional

    if loss_notional <= 0:
        # 손실방향 포지션 없음 → ASYM 의미 없음 → 거부
        return False, "ASYM_NO_LOSS_SIDE_TO_COVER"

    ratio = cover_notional / loss_notional

    if ratio > cover_ratio_max:
        return False, (
            f"ASYM_COVER_RATIO_OVERFLOW "
            f"ratio={ratio:.2f}>{cover_ratio_max} "
            f"(cover={cover_notional:.0f} loss={loss_notional:.0f})"
        )

    # ratio < min은 커버가 약한 것 → 진입 허용 (커버 강화 목적)
    return True, f"ASYM_COVER_OK ratio={ratio:.2f}"
