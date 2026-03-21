"""
[DEPRECATED — NOT USED]
V9 Hedge Engine  (v9.1 — 무한 헷지)
이 파일은 v9.6에서 hedge 전략 제거로 인해 더 이상 import되지 않음.
참조 보존 목적으로 유지.
=====================================

작동 원리 (무한 사이클):
  1) T4 진입 즉시 → planners.plan_hedge() 가 HEDGE intent 생성
     - 반대방향 기존 포지션 → mark_only (플래그)
     - 반대방향 비어있으면 → 유니버스 1순위 신규 오픈
  2) 이 엔진이 매 틱 수행하는 작업:
     A) 익절 감시: 헷지 포지션 roi ≥ +2.5%
        → 소스 CLOSE + 헷지 CLOSE (동시)
     B) Stage 2 격상: 헷지 포지션 자체가 T4 물림
        → hedge_stage=2, 목표 2.4x
     C) top-up DCA: 목표 배수 미달 시 기존 헷지 포지션 증량
     D) 전환: 목표 달성 + roi≥1% → HEDGE_TO_NORMAL

설계 원칙:
  - 소스 + 헷지는 항상 쌍 (source_sym 으로 연결)
  - 익절 시 반드시 양쪽 동시 청산 (orphan 방지)
  - CorrGuard 면제: get_active_hedge_sources() 제공
  - DCA: 헷지 포지션도 일반 DCA 타겟 적용 (dca_engine에서 처리)
"""
import uuid
from typing import List, Dict, Set

from v9.types import Intent, IntentType, MarketSnapshot
from v9.execution.position_book import iter_positions, is_active
from v9.config import (
    DCA_WEIGHTS,
    LEVERAGE,
    HEDGE_STAGE1_MULTIPLIER,
    HEDGE_STAGE2_MULTIPLIER,
    HEDGE_MAX_MULTIPLIER,
    HEDGE_PROFIT_CLOSE_PCT,
    HEDGE_OPEN_CORR_MIN,
)
from v9.utils.utils_math import calc_roi_pct


def _tid() -> str:
    return str(uuid.uuid4())[:8]


def _get_hedge_stage(p: dict) -> int:
    """포지션의 hedge_stage (기본 1)."""
    return int(p.get("hedge_stage", 1) or 1)


def _target_multiplier(stage: int) -> float:
    """stage별 목표 배수."""
    if stage >= 2:
        return HEDGE_STAGE2_MULTIPLIER   # 2.4x
    return HEDGE_STAGE1_MULTIPLIER       # 1.4x


# ═════════════════════════════════════════════════════════════════
# 메인 함수
# ═════════════════════════════════════════════════════════════════
def generate_hedge_intents(
    snapshot: MarketSnapshot,
    st: Dict,
) -> List[Intent]:
    """
    매 틱 호출.  소스-헷지 그룹별로 A/B/C/D 처리.
    """
    intents: List[Intent] = []
    prices   = getattr(snapshot, "all_prices", {}) or {}
    corr_map = getattr(snapshot, "correlations", {}) or {}

    # ── 소스별 헷지 포지션 그룹핑 ────────────────────────────────
    #   source_map[source_sym] = {
    #     "hpos":      [{"sym","p","px","amt"}, ...],
    #     "h_exp":     float (헷지 exposure 합산),
    #     "h_side":    str,
    #     "max_stage": int,
    #     "s_not":     float (소스 notional),
    #     "s_p":       dict  (소스 포지션),
    #     "s_px":      float,
    #     "s_amt":     float,
    #   }
    source_map: Dict[str, dict] = {}

    for sym, sym_st in (st or {}).items():
        if not (isinstance(sym_st, dict) and is_active(sym_st)):
            continue
        for _, p in iter_positions(sym_st):
         p = p or {}

        if p.get("hedge_mode"):
            # ── 헷지 포지션 ──
            src = p.get("source_sym", "")
            if not src:
                continue
            if src not in source_map:
                source_map[src] = {
                    "hpos": [], "h_exp": 0.0,
                    "h_side": p.get("side", "sell"),
                    "max_stage": 1,
                }
            px  = float(prices.get(sym, 0.0) or 0.0)
            amt = float(p.get("amt", 0.0) or 0.0)
            source_map[src]["h_exp"] += amt * px
            source_map[src]["hpos"].append(
                {"sym": sym, "p": p, "px": px, "amt": amt}
            )
            stg = _get_hedge_stage(p)
            if stg > source_map[src]["max_stage"]:
                source_map[src]["max_stage"] = stg

        else:
            # ── 소스 포지션 ──
            if sym not in source_map:
                source_map[sym] = {
                    "hpos": [], "h_exp": 0.0,
                    "h_side": "sell" if p.get("side") == "buy" else "buy",
                    "max_stage": 1,
                }
            px  = float(prices.get(sym, 0.0) or 0.0)
            amt = float(p.get("amt", 0.0) or 0.0)
            source_map[sym]["s_not"] = amt * px
            source_map[sym]["s_p"]   = p
            source_map[sym]["s_px"]  = px
            source_map[sym]["s_amt"] = amt

    # ── 소스별 A/B/C/D 처리 ──────────────────────────────────────
    for source_sym, info in source_map.items():
        s_not    = info.get("s_not", 0.0)
        h_exp    = info.get("h_exp", 0.0)
        hpos     = info.get("hpos", [])
        h_side   = info.get("h_side", "sell")
        s_p      = info.get("s_p", {})
        s_px     = info.get("s_px", 0.0)
        s_amt    = info.get("s_amt", 0.0)
        max_stg  = info.get("max_stage", 1)

        # 소스가 없거나 헷지가 없으면 스킵
        if s_not <= 0 or not hpos:
            continue

        # ────────────────────────────────────────────────────────
        # A) 익절 감시:  헷지 roi ≥ HEDGE_PROFIT_CLOSE_PCT
        #    → 소스 CLOSE + 헷지 CLOSE (동시)
        # ────────────────────────────────────────────────────────
        profit_triggered = False
        for hp_info in hpos:
            hsym = hp_info["sym"]
            hp   = hp_info["p"]
            hpx  = hp_info["px"]
            if hpx <= 0:
                continue

            h_roi = calc_roi_pct(
                hp.get("ep", 0.0), hpx,
                hp.get("side", "sell"), LEVERAGE,
            )

            if h_roi >= HEDGE_PROFIT_CLOSE_PCT:
                profit_triggered = True

                # 헷지 CLOSE
                intents.append(Intent(
                    trace_id=_tid(),
                    intent_type=IntentType.CLOSE,
                    symbol=hsym,
                    side="sell" if hp.get("side") == "buy" else "buy",
                    qty=float(hp.get("amt", 0.0)),
                    price=hpx,
                    reason=f"HEDGE_PROFIT_{h_roi:.1f}pct_CLOSE_HEDGE",
                    metadata={
                        "source_sym": source_sym,
                        "hedge_profit_close": True,
                    },
                ))
                # 소스 CLOSE
                if s_px > 0 and s_amt > 0:
                    intents.append(Intent(
                        trace_id=_tid(),
                        intent_type=IntentType.CLOSE,
                        symbol=source_sym,
                        side="sell" if s_p.get("side") == "buy" else "buy",
                        qty=s_amt,
                        price=s_px,
                        reason=f"HEDGE_PROFIT_{h_roi:.1f}pct_CLOSE_SOURCE",
                        metadata={
                            "triggered_by": hsym,
                            "hedge_profit_close": True,
                        },
                    ))
                break  # 이 소스 그룹 처리 완료

        if profit_triggered:
            # 같은 소스의 다른 헷지 포지션은 orphan으로 정리됨
            continue

        # ────────────────────────────────────────────────────────
        # B) Stage 2 격상:  헷지 포지션 dca_level ≥ 4 + stage < 2
        # ────────────────────────────────────────────────────────
        for hp_info in hpos:
            hp = hp_info["p"]
            hp_dca   = int(hp.get("dca_level", 1) or 1)
            hp_stage = _get_hedge_stage(hp)

            if hp_dca >= 4 and hp_stage < 2:
                # ★ in-place 직접 수정 (qty=0 Intent 대신)
                hp["hedge_stage"] = 2
                print(f"[HedgeEngine] Stage2 격상: {hp_info['sym']}")
                if max_stg < 2:
                    max_stg = 2

        # ────────────────────────────────────────────────────────
        # 목표 배수 계산
        # ────────────────────────────────────────────────────────
        target_mult = _target_multiplier(max_stg)
        want = s_not * target_mult

        # ────────────────────────────────────────────────────────
        # D) 목표 달성 + roi ≥ 1% → HEDGE_TO_NORMAL
        # ────────────────────────────────────────────────────────
        if h_exp >= want:
            for hp_info in hpos:
                hp  = hp_info["p"]
                hpx = hp_info["px"]
                if hpx <= 0:
                    continue
                h_roi = calc_roi_pct(
                    hp.get("ep", 0.0), hpx,
                    hp.get("side", "sell"), LEVERAGE,
                )
                if h_roi >= 1.0:
                    # ★ in-place 직접 수정 (qty=0 Intent 대신)
                    hp["hedge_mode"]  = False
                    hp["was_hedge"]   = True
                    hp["source_sym"]  = ""
                    hp["hedge_stage"] = 1
                    hp["tp1_done"]    = False
                    hp["step"]        = 0
                    hp["dca_targets"] = []   # 헷지용 타겟 제거
                    print(f"[HedgeEngine] HEDGE_TO_NORMAL: {hp_info['sym']} roi={h_roi:.2f}%")
            continue  # 이 소스 그룹 처리 완료

        # ────────────────────────────────────────────────────────
        # C) 목표 미달 → top-up DCA
        # ────────────────────────────────────────────────────────
        if float(corr_map.get(source_sym, 1.0)) < HEDGE_OPEN_CORR_MIN:
            continue

        need = want - h_exp
        if need <= 0:
            continue

        n_syms = len(hpos)
        if n_syms <= 0:
            continue

        dca_w   = DCA_WEIGHTS
        total_w = sum(dca_w)

        for hp_info in hpos:
            hp  = hp_info["p"]
            hpx = hp_info["px"]
            if hpx <= 0:
                continue
            cur_level  = int(hp.get("dca_level", 1) or 1)
            next_level = min(cur_level + 1, len(dca_w))
            # 단계별 notional 비중
            step_notional = (s_not / n_syms) * (dca_w[next_level - 1] / total_w)
            add_notional  = min(need / n_syms, step_notional)
            if add_notional <= 0:
                continue
            add_qty = add_notional / hpx
            if add_qty <= 0:
                continue
            intents.append(Intent(
                trace_id=_tid(),
                symbol=hp_info["sym"],
                side=h_side,
                intent_type=IntentType.DCA,
                qty=add_qty,
                price=None,
                reason=f"HEDGE_TOPUP_T{next_level}_stg{max_stg}",
                metadata={
                    "hedge_mode": True,
                    "source_sym": source_sym,
                    "tier": next_level,
                    "reason": "HEDGE_TOPUP",
                },
            ))

    return intents


# ═════════════════════════════════════════════════════════════════
# CorrGuard 면제용 유틸리티
# ═════════════════════════════════════════════════════════════════
def get_active_hedge_sources(st: Dict) -> Set[str]:
    """
    현재 헷지 연결이 활성화된 소스 심볼 집합을 반환.
    CorrGuard에서 이 심볼들을 면제하기 위해 사용.
    """
    sources: Set[str] = set()
    for sym, sym_st in (st or {}).items():
        if not (isinstance(sym_st, dict) and is_active(sym_st)):
            continue
        for _, p in iter_positions(sym_st):
         p = p or {}
        if p.get("hedge_mode"):
            src = p.get("source_sym", "")
            if src:
                sources.add(src)
    return sources
