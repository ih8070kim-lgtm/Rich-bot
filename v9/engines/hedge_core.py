"""
hedge_core.py — SOURCE_LINKED_HEDGE_CORE 모듈
==================================================
v10.10 → v10.11 분리

진입: 스큐 ≥ 12%, heavy side 소스 기반 반대방향
운영: TP1 없음, trailing 없음, DCA는 손실일 때만
청산: 소스 TP1/청산 시 → 수익이면 같이 청산, 손실이면 CORE_MR 전환
     HARD_SL은 planners.py에서 유지 (독립)
"""

import time
import uuid
from typing import Dict, List, Tuple, Optional

from v9.types import Intent, IntentType, MarketSnapshot
from v9.config import (
    LEVERAGE, DCA_WEIGHTS,
    TOTAL_MAX_SLOTS, MAX_LONG, MAX_SHORT,
)
from v9.execution.position_book import get_p, iter_positions
from v9.risk.slot_manager import count_slots
from v9.utils.utils_math import calc_roi_pct


def _tid():
    return str(uuid.uuid4())[:8]


def _build_hedge_dca_targets(entry_p: float, side: str, grid_notional: float, regime: str = "LOW") -> list:
    """헷지 포지션용 DCA 타겟 생성. planners._build_dca_targets 위임."""
    try:
        from v9.strategy.planners import _build_dca_targets
        return _build_dca_targets(entry_p, side, grid_notional, regime)
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════
# 스큐 계산
# ═══════════════════════════════════════════════════════════════

def calc_skew(st: dict, total_cap: float) -> Tuple[float, float, float]:
    """롱/숏 마진 비율 → 스큐 반환.
    Returns: (skew, long_margin_ratio, short_margin_ratio)
    """
    if total_cap <= 0:
        return 0.0, 0.0, 0.0
    long_m = sum(
        float(get_p(st.get(s, {}), "buy").get("amt", 0)) *
        float(get_p(st.get(s, {}), "buy").get("ep", 0)) / LEVERAGE
        for s in st if isinstance(get_p(st.get(s, {}), "buy"), dict)
    ) / total_cap
    short_m = sum(
        float(get_p(st.get(s, {}), "sell").get("amt", 0)) *
        float(get_p(st.get(s, {}), "sell").get("ep", 0)) / LEVERAGE
        for s in st if isinstance(get_p(st.get(s, {}), "sell"), dict)
    ) / total_cap
    return abs(long_m - short_m), long_m, short_m


# ═══════════════════════════════════════════════════════════════
# 진입 — plan_open에서 호출
# ═══════════════════════════════════════════════════════════════

def plan_hedge_core_entry(
    snapshot: MarketSnapshot,
    st: Dict,
    skew: float,
    long_margin: float,
    short_margin: float,
    total_cap: float,
    btc_regime: str,
    asym_syms: set,
    skew_thresh: float = 0.12,
) -> List[Intent]:
    """스큐 기반 CORE_HEDGE 진입.
    Returns: intents 리스트 + asym_syms 업데이트
    """
    intents: List[Intent] = []

    if skew < skew_thresh:
        return intents

    heavy_side = "buy" if long_margin > short_margin else "sell"
    hedge_side = "sell" if heavy_side == "buy" else "buy"

    # ── 소스 후보: heavy side, step=0, CORE만 ──
    # 정렬: DCA 깊은 순 → ROI 낮은 순 (T2 없으면 ROI 낮은 T1 우선)
    src_candidates = []
    for sym, sym_st in st.items():
        hp = get_p(sym_st, heavy_side)
        if not isinstance(hp, dict):
            continue
        if hp.get("role") in ("HEDGE", "SOFT_HEDGE", "INSURANCE_SH", "CORE_HEDGE"):
            continue
        if hp.get("step", 0) >= 1:
            continue  # trailing 중 = 소스 부적격
        # ★ v10.11b: T5 소스에는 헷지 안 붙임 (T5 독립게임)
        if int(hp.get("dca_level", 1) or 1) >= 5:
            continue
        # ★ v10.12: T1 소스 제외 (진입 초기, 구조적 스큐 판단 이름)
        if int(hp.get("dca_level", 1) or 1) <= 1:
            continue
        cp = float((snapshot.all_prices or {}).get(sym, 0.0))
        if cp <= 0:
            continue
        roi = calc_roi_pct(float(hp.get("ep", 0)), cp, heavy_side, LEVERAGE)
        dca = int(hp.get("dca_level", 1) or 1)
        # ★ v10.12: T2~T4 소스는 ROI ≤ -1.2%일 때만 (DCA -1.5% 전에 발동, MDD 개선)
        if roi > -1.2:
            continue
        src_candidates.append((sym, hp, cp, roi, dca))

    # DCA 깊은 순 → ROI 낮은 순
    src_candidates.sort(key=lambda x: (-x[4], x[3]))

    slots = count_slots(st)
    weak_count = slots.risk_short if hedge_side == "sell" else slots.risk_long
    weak_max = MAX_SHORT if hedge_side == "sell" else MAX_LONG

    for src_sym, src_p, src_cp, src_roi, src_dca in src_candidates:
        # ★ v10.12: 슬롯 없으면 시도조차 안 함
        if weak_count >= weak_max or weak_count >= 4:
            break
        if src_sym in asym_syms:
            continue

        # ★ v10.12: 같은 심볼 반대방향에 이미 포지션 있으면 스킵
        _existing = get_p(st.get(src_sym, {}), hedge_side)
        if isinstance(_existing, dict):
            continue

        # T2 사이즈 (T1+T2 누적 비중)
        grid = (total_cap / TOTAL_MAX_SLOTS) * LEVERAGE
        cum_w = sum(DCA_WEIGHTS[:2]) / sum(DCA_WEIGHTS)
        notional = grid * cum_w
        qty = notional / src_cp
        if qty <= 0:
            continue

        intents.append(Intent(
            trace_id=_tid(),
            intent_type=IntentType.OPEN,
            symbol=src_sym,
            side=hedge_side,
            qty=qty,
            price=src_cp,
            reason=f"HEDGE_CORE(src={src_sym},roi={src_roi:.1f}%,dca=T{src_dca},skew={skew*100:.0f}%)",
            metadata={
                "atr": 0.0,
                "dca_targets": [t for t in _build_hedge_dca_targets(src_cp, hedge_side, grid, btc_regime) if t.get("tier", 0) > 2],
                "role": "CORE_HEDGE",
                "entry_type": "HEDGE_CORE",
                "source_sym": src_sym,
                "source_side": heavy_side,
                "dca_level": 2,  # ★ T1+T2 사이즈로 진입 → T2 완료 상태
                "positionSide": "LONG" if hedge_side == "buy" else "SHORT",
                "locked_regime": btc_regime,
            },
        ))
        asym_syms.add(src_sym)
        weak_count += 1
        print(f"[HEDGE_CORE] {src_sym} {hedge_side} src_roi={src_roi:.1f}% "
              f"src_dca=T{src_dca} skew={skew*100:.0f}% ${notional:.0f}")

    return intents


# ═══════════════════════════════════════════════════════════════
# 관리 — 소스 상태 감시 + 청산/전환
# ═══════════════════════════════════════════════════════════════

def plan_hedge_core_manage(
    snapshot: MarketSnapshot,
    st: Dict,
) -> List[Intent]:
    """CORE_HEDGE 포지션 관리.

    규칙 (v10.11b):
    1. 소스가 TP1 했거나 완전 청산:
       → 손익 무관하게 CORE_MR로 전환 (독립 운영)
       → 자체 DCA TP1 라이프사이클을 탐 (TP1 → trailing → exit)
    2. 그 외: 유지 (HARD_SL은 plan_force_close에서 처리)

    이유: 소스 TP1 시점에 헷지 ROI +0.0~0.3% 수준에서 바로 청산하면
          수수료 제하면 본전치기 — 자체 TP1(T2=1.5%)까지 기다려야 수익 확보
    """
    intents: List[Intent] = []

    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue

        for pos_side, hedge in iter_positions(sym_st):
            if not isinstance(hedge, dict):
                continue
            if hedge.get("role") != "CORE_HEDGE":
                continue

            source_sym = hedge.get("source_sym", "")
            source_side = hedge.get("source_side", "")
            if not source_sym or not source_side:
                source_side = "buy" if pos_side == "sell" else "sell"

            # 소스 포지션 확인
            src_st = st.get(source_sym, {})
            src_p = get_p(src_st, source_side)

            # 현재 헷지 ROI
            cp = float((snapshot.all_prices or {}).get(sym, 0.0))
            if cp <= 0:
                continue
            hedge_ep = float(hedge.get("ep", 0.0) or 0.0)
            hedge_roi = calc_roi_pct(hedge_ep, cp, pos_side, LEVERAGE) if hedge_ep > 0 else 0.0

            # ── 소스 상태 확인 ──
            source_gone = not isinstance(src_p, dict)
            source_tp1 = isinstance(src_p, dict) and bool(src_p.get("tp1_done", False))

            if not source_gone and not source_tp1:
                # ★ v10.12: 듀얼 프로핏 TP1 — 양쪽 ROI ≥ 0.3% → 40% 확정 + 60% trailing
                DUAL_PROFIT_THRESH = 0.3
                src_ep = float(src_p.get("ep", 0.0) or 0.0) if isinstance(src_p, dict) else 0
                src_roi = calc_roi_pct(src_ep, cp, source_side, LEVERAGE) if src_ep > 0 else 0.0

                if src_roi >= DUAL_PROFIT_THRESH and hedge_roi >= DUAL_PROFIT_THRESH:
                    _TP1_RATIO = 0.40
                    # 소스 TP1
                    src_amt = float(src_p.get("amt", 0.0))
                    src_close_qty = src_amt * _TP1_RATIO
                    src_close_side = "sell" if source_side == "buy" else "buy"
                    if src_close_qty > 0 and not src_p.get("tp1_done"):
                        intents.append(Intent(
                            trace_id=_tid(),
                            intent_type=IntentType.TP1,
                            symbol=source_sym,
                            side=src_close_side,
                            qty=src_close_qty,
                            price=cp,
                            reason=f"DUAL_PROFIT_SRC(s={src_roi:+.1f}%,h={hedge_roi:+.1f}%)",
                            metadata={"roi_gross": src_roi, "_expected_role": src_p.get("role", "")},
                        ))
                    # 헷지 TP1
                    hedge_amt = float(hedge.get("amt", 0.0))
                    hedge_close_qty = hedge_amt * _TP1_RATIO
                    hedge_close_side = "sell" if pos_side == "buy" else "buy"
                    if hedge_close_qty > 0 and not hedge.get("tp1_done"):
                        intents.append(Intent(
                            trace_id=_tid(),
                            intent_type=IntentType.TP1,
                            symbol=sym,
                            side=hedge_close_side,
                            qty=hedge_close_qty,
                            price=cp,
                            reason=f"DUAL_PROFIT_HDG(s={src_roi:+.1f}%,h={hedge_roi:+.1f}%)",
                            metadata={"roi_gross": hedge_roi, "_expected_role": "CORE_HEDGE"},
                        ))
                    if src_close_qty > 0 or hedge_close_qty > 0:
                        print(f"[DUAL_PROFIT] {sym} src_roi={src_roi:+.1f}% hedge_roi={hedge_roi:+.1f}% "
                              f"→ 양쪽 TP1 (40% 확정 + 60% trailing)")

                # ★ v10.11b: 소스 T5 + 헷지 ROI ≥ 5% → 헷지 익절
                # 소스는 T5 독립게임으로, 헷지 수익 확정
                src_dca = int(src_p.get("dca_level", 1) or 1) if isinstance(src_p, dict) else 0
                if src_dca >= 5 and hedge_roi >= 5.0:
                    hedge_amt = float(hedge.get("amt", 0.0))
                    if hedge_amt > 0:
                        close_side = "sell" if pos_side == "buy" else "buy"
                        intents.append(Intent(
                            trace_id=_tid(),
                            intent_type=IntentType.FORCE_CLOSE,
                            symbol=sym,
                            side=close_side,
                            qty=hedge_amt,
                            price=cp,
                            reason=f"HC_T5_PROFIT(roi={hedge_roi:+.1f}%,src_T{src_dca})",
                            metadata={"_expected_role": "CORE_HEDGE"},
                        ))
                        print(f"[HEDGE_CORE] {sym} {pos_side} T5 익절: roi={hedge_roi:+.1f}% → 소스 독립게임")
                continue  # 소스 아직 건재 → 유지

            # ── v10.11b: 손익 무관 CORE_MR 전환 ──
            # 자체 TP1 라이프사이클로 수익 극대화
            hedge["role"] = "CORE_MR"
            hedge["source_sym"] = ""
            hedge["source_side"] = ""
            hedge["entry_type"] = "MR"

            # ★ v10.11b: 전환 시 dca_targets 재생성 (비어있으면 DCA 안 나감)
            try:
                from v9.strategy.planners import _build_dca_targets
                _h_ep = float(hedge.get("ep", 0) or 0)
                _h_side = hedge.get("side", "buy")
                _h_amt = float(hedge.get("amt", 0) or 0)
                _h_dca = int(hedge.get("dca_level", 1) or 1)
                _h_notional = _h_ep * _h_amt
                _cum_w = sum(DCA_WEIGHTS[:_h_dca]) if _h_dca <= len(DCA_WEIGHTS) else sum(DCA_WEIGHTS)
                _total_w = sum(DCA_WEIGHTS)
                _grid_est = _h_notional / (_cum_w / _total_w) if _cum_w > 0 else _h_notional * 5
                _all_targets = _build_dca_targets(_h_ep, _h_side, _grid_est, hedge.get("locked_regime", "LOW"))
                hedge["dca_targets"] = [t for t in _all_targets if t.get("tier", 0) > _h_dca]
                print(f"[HEDGE_CORE→MR] {sym} dca_targets 재생성: {len(hedge['dca_targets'])}개 (T{_h_dca+1}부터)")
            except Exception as _e:
                print(f"[HEDGE_CORE→MR] {sym} dca_targets 생성 실패(무시): {_e}")

            reason = "HC_CONVERT_TP1" if source_tp1 else "HC_CONVERT_GONE"
            print(f"[HEDGE_CORE→MR] {sym} {pos_side} 전환: {reason} "
                  f"roi={hedge_roi:+.1f}% → 독립 코어로 운영 (자체 TP1 대기)")

    return intents


# ═══════════════════════════════════════════════════════════════
# DCA 가드 — planners.py DCA 섹션에서 호출
# ═══════════════════════════════════════════════════════════════

def is_hedge_dca_blocked(p: dict, snapshot: MarketSnapshot, symbol: str) -> bool:
    """CORE_HEDGE 수익 중이면 DCA 차단.
    True = DCA 금지, False = DCA 허용
    """
    if p.get("role") != "CORE_HEDGE":
        return False

    cp = float((snapshot.all_prices or {}).get(symbol, 0.0))
    ep = float(p.get("ep", 0.0) or 0.0)
    if ep <= 0 or cp <= 0:
        return False

    side = p.get("side", "buy")
    roi = calc_roi_pct(ep, cp, side, LEVERAGE)

    if roi >= 0:
        # 수익 중 → DCA 금지 (보험이 본체 되면 안 됨)
        return True

    return False
