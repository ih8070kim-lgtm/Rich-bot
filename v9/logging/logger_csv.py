"""
V9 Logger CSV
6종 CSV 로그 기록기 (trace_id 연결)
"""
import csv
import os
from datetime import datetime, timezone

from v9.config import LOG_DIR
from v9.logging.schemas import (
    FILLS_COLUMNS,
    FUNDING_COLUMNS,
    HEDGE_SIM_COLUMNS,
    INTENTS_COLUMNS,
    ORDERS_COLUMNS,
    POSITIONS_COLUMNS,
    RISK_COLUMNS,
    TRADES_COLUMNS,
    UNIVERSE_COLUMNS,
    DCA_SIM_COLUMNS,
    SKEW_COLUMNS,
)


def _ensure_dir():
    os.makedirs(LOG_DIR, exist_ok=True)


def _log_path(filename: str) -> str:
    _ensure_dir()
    return os.path.join(LOG_DIR, filename)


def _now_str() -> str:
    # ★ V10.31AK: UTC 명시 — 서버 타임존 독립, Binance/ccxt와 일치
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


# ★ V10.29c: 시스템 이벤트 로그 (부팅/복원/BB/트림 등 — CSV 추출 가능)
def log_system(tag: str, msg: str):
    """log_system.csv에 한 줄 추가. 추출 도구에서 확인 가능."""
    try:
        fp = _log_path("log_system.csv")
        is_new = not os.path.exists(fp)
        with open(fp, "a", newline="") as f:
            w = csv.writer(f)
            if is_new:
                w.writerow(["timestamp", "tag", "message"])
            w.writerow([_now_str(), tag, msg])
    except Exception:
        pass


def _append_csv(filepath: str, columns: list, row: dict):
    """헤더 없으면 자동 생성 후 append"""
    try:
        write_header = not os.path.exists(filepath) or os.path.getsize(filepath) == 0
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
            if write_header:
                w.writeheader()
            w.writerow(row)
    except Exception as e:
        print(f"[logger_csv] append 실패 {filepath}: {e}")


# ── log_intents ─────────────────────────────────────────────────
def log_intent(
    trace_id: str,
    intent_type: str,
    symbol: str,
    side: str,
    qty: float,
    price: float | None,
    reason: str,
    approved: bool,
    reject_code: str,
    role: str = "",
    source_sym: str = "",
):
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "intent_type": intent_type,
        "symbol": symbol,
        "side": side,
        "qty": round(qty, 8),
        "price": round(price, 8) if price else "",
        "reason": reason,
        "approved": approved,
        "reject_code": reject_code,
        "role": role,
        "source_sym": source_sym,
    }
    _append_csv(_log_path("log_intents.csv"), INTENTS_COLUMNS, row)


# ── log_risk ────────────────────────────────────────────────────
def log_risk(
    trace_id: str,
    symbol: str,
    intent_type: str,
    reject_code: str,
    margin_ratio: float,
    risk_slots_total: int,
    risk_slots_long: int,
    risk_slots_short: int,
    step: int = 0,
    dca_level: int = 1,
    note: str = "",
):
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "symbol": symbol,
        "intent_type": intent_type,
        "reject_code": reject_code,
        "margin_ratio": round(margin_ratio, 4),
        "risk_slots_total": risk_slots_total,
        "risk_slots_long": risk_slots_long,
        "risk_slots_short": risk_slots_short,
        "step": step,
        "dca_level": dca_level,
        "note": note,
    }
    _append_csv(_log_path("log_risk.csv"), RISK_COLUMNS, row)


# ── log_orders ──────────────────────────────────────────────────
def log_order(
    trace_id: str,
    symbol: str,
    side: str,
    order_type: str,
    qty: float,
    price: float | None,
    tag: str,
    order_id: str | None,
    status: str,
):
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "qty": round(qty, 8),
        "price": round(price, 8) if price else "",
        "tag": tag,
        "order_id": order_id or "",
        "status": status,
    }
    _append_csv(_log_path("log_orders.csv"), ORDERS_COLUMNS, row)


# ── log_fills ───────────────────────────────────────────────────
def log_fill(
    trace_id: str,
    symbol: str,
    side: str,
    avg_price: float,
    filled_qty: float,
    tag: str,
    order_id: str | None,
    ep: float = 0.0,
    pnl_usdt: float = 0.0,
    roi_pct: float = 0.0,
    dca_level: int = 0,
    hold_sec: float = 0.0,
):
    notional = avg_price * filled_qty if avg_price > 0 else 0.0
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "symbol": symbol,
        "side": side,
        "avg_price": round(avg_price, 8),
        "filled_qty": round(filled_qty, 8),
        "notional": round(notional, 4),
        "tag": tag,
        "order_id": order_id or "",
        "ep": round(ep, 8) if ep else "",
        "pnl_usdt": round(pnl_usdt, 4) if pnl_usdt else "",
        "roi_pct": round(roi_pct, 4) if roi_pct else "",
        "dca_level": dca_level if dca_level else "",
        "hold_sec": round(hold_sec) if hold_sec else "",
    }
    _append_csv(_log_path("log_fills.csv"), FILLS_COLUMNS, row)


# ── log_positions ───────────────────────────────────────────────
def log_position(
    trace_id: str,
    symbol: str,
    side: str,
    ep: float,
    amt: float,
    dca_level: int,
    step: int,
    roi_pct: float,
    max_roi_seen: float,
    trailing_on: bool,
    hedge_mode: bool,
    tag: str,
    curr_price: float = 0.0,
    notional: float = 0.0,
    unrealized_pnl: float = 0.0,
    role: str = "",
    source_sym: str = "",
):
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "symbol": symbol,
        "side": side,
        "ep": round(ep, 8),
        "amt": round(amt, 8),
        "dca_level": dca_level,
        "step": step,
        "roi_pct": round(roi_pct, 4),
        "max_roi_seen": round(max_roi_seen, 4),
        "trailing_on": trailing_on,
        "hedge_mode": hedge_mode,
        "tag": tag,
        "curr_price": round(curr_price, 8) if curr_price else "",
        "notional": round(notional, 4) if notional else "",
        "unrealized_pnl": round(unrealized_pnl, 4) if unrealized_pnl else "",
        "role": role,
        "source_sym": source_sym,
    }
    _append_csv(_log_path("log_positions.csv"), POSITIONS_COLUMNS, row)


# ── log_trades (신규) ───────────────────────────────────────────
def log_trade(
    trace_id: str,
    symbol: str,
    side: str,
    ep: float,
    exit_price: float,
    amt: float,
    pnl_usdt: float,
    roi_pct: float,
    dca_level: int,
    hold_sec: float,
    reason: str,
    hedge_mode: bool = False,
    was_hedge: bool = False,
    max_roi_seen: float = 0.0,
    entry_type: str = "MR",
    role: str = "",
    source_sym: str = "",
    fee_usdt: float = 0.0,  # ★ V10.31d: 청산 수수료 합계
    t1_max_roi_pre_dca: float = 0.0,  # ★ V10.31e: T1 DCA 직전 max_roi 보존값
    worst_roi_seen: float = 0.0,  # ★ V10.31j: 최종 tier 구간 worst_roi (디펜스 튜닝용)
):
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "symbol": symbol,
        "side": side,
        "ep": round(ep, 8),
        "exit_price": round(exit_price, 8),
        "amt": round(amt, 8),
        "pnl_usdt": round(pnl_usdt, 4),
        "fee_usdt": round(fee_usdt, 6),  # ★ V10.31d
        "roi_pct": round(roi_pct, 4),
        "dca_level": dca_level,
        "hold_sec": round(hold_sec),
        "reason": reason,
        "hedge_mode": hedge_mode,
        "was_hedge": was_hedge,
        "max_roi_seen": round(max_roi_seen, 4),
        "entry_type": entry_type,
        "role": role,
        "source_sym": source_sym,
        "t1_max_roi_pre_dca": round(t1_max_roi_pre_dca, 4),  # ★ V10.31e
        "worst_roi_seen": round(worst_roi_seen, 4),  # ★ V10.31j
    }
    _append_csv(_log_path("log_trades.csv"), TRADES_COLUMNS, row)


# ── log_funding (★ V10.31d: 펀딩비 누수량 측정) ─────────────────
def log_funding(
    symbol: str,
    funding_usdt: float,
    funding_rate: float = 0.0,
    position_amt: float = 0.0,
    event_time: str = "",
):
    """펀딩 이벤트 1건 = 1행. fetch_funding_history에서 받은 각 레코드를 append."""
    row = {
        "time": event_time or _now_str(),
        "symbol": symbol,
        "funding_usdt": round(funding_usdt, 6),
        "funding_rate": round(funding_rate, 8),
        "position_amt": round(position_amt, 8),
    }
    _append_csv(_log_path("log_funding.csv"), FUNDING_COLUMNS, row)


# ── log_skew (★ V10.31AM3 hotfix-6: 부활 — 스큐+PTP 결합 검증 인프라)
def log_skew(
    trace_id: str,
    skew: float,
    long_m: float,
    short_m: float,
    skew_signed: float,
    long_count: int,
    short_count: int,
    balance: float,
    peak_balance: float,
    drop_pct: float,
    ptp_armed: bool,
    urgency: float,
):
    """스큐 + 잔고 drop 시계열 로그.

    사용자 가설 검증용: "균형 깨짐 + drop 발생 = 추세 시그널 = PTP 정확 타이밍"
    4주 데이터 누적 후 스큐+PTP 결합 효과 분석 가능.
    """
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "skew": round(skew, 4),
        "long_m": round(long_m, 4),
        "short_m": round(short_m, 4),
        "skew_signed": round(skew_signed, 4),
        "long_count": int(long_count),
        "short_count": int(short_count),
        "balance": round(balance, 2),
        "peak_balance": round(peak_balance, 2),
        "drop_pct": round(drop_pct, 4),
        "ptp_armed": bool(ptp_armed),
        "urgency": round(urgency, 2),
    }
    _append_csv(_log_path("log_skew.csv"), SKEW_COLUMNS, row)


# ── log_universe ────────────────────────────────────────────────
def log_universe(
    trace_id: str,
    top10: list,
    long_4: list,
    short_4: list,
    regime: str,
    btc_price: float,
    note: str = "",
):
    row = {
        "time": _now_str(),
        "trace_id": trace_id,
        "top10": "|".join(top10),
        "long_4": "|".join(long_4),
        "short_4": "|".join(short_4),
        "regime": regime,
        "btc_price": round(btc_price, 2),
        "note": note,
    }
    _append_csv(_log_path("log_universe.csv"), UNIVERSE_COLUMNS, row)


# ── log_hedge_sim (★ V10.31e-6) ────────────────────────────────
def log_hedge_sim(
    mr_sym: str,
    mr_side: str,
    sim_side: str,
    trend_sym: str,
    trend_side: str,
    sim_t1_ep: float,
    sim_final_ep: float,
    sim_final_tier: int,
    sim_notional_t1: float,
    sim_final_roi: float,
    sim_max_roi: float,
    sim_close_reason: str,
    hold_sec: int,
):
    """가상 MR 헷지 시뮬 종료 시 1행 기록. 실전 영향 0, 관찰 전용."""
    row = {
        "time": _now_str(),
        "mr_sym": mr_sym,
        "mr_side": mr_side,
        "sim_side": sim_side,
        "trend_sym": trend_sym,
        "trend_side": trend_side,
        "sim_t1_ep": round(sim_t1_ep, 8),
        "sim_final_ep": round(sim_final_ep, 8),
        "sim_final_tier": int(sim_final_tier),
        "sim_notional_t1": round(sim_notional_t1, 2),
        "sim_final_roi": round(sim_final_roi, 4),
        "sim_max_roi": round(sim_max_roi, 4),
        "sim_close_reason": sim_close_reason,
        "hold_sec": int(hold_sec),
    }
    _append_csv(_log_path("log_hedge_sim.csv"), HEDGE_SIM_COLUMNS, row)


# ── log_dca_sim (★ V10.31AM3: DCA 폭 변경 백테스트용 시계열 가격 로그) ─────
def log_dca_sim(
    trace_id: str,
    symbol: str,
    side: str,
    t1_ep: float,
    t1_open_ts: float,
    t1_amt: float,
    mark_price: float,
    t1_roi_pct: float,
    actual_tier: int,
    actual_blended_ep: float,
    actual_amt: float,
    balance: float = 0.0,
    active_count: int = 0,
):
    """DCA 백테스트용 시계열 가격 로그.
    
    60초 throttle 호출 — runner.py에서 last_sim_ts 관리.
    실거래 영향 0. 사후 백테스트로 임의 DCA 파라미터 시뮬 가능.
    
    백테스트 사용:
        df = pd.read_csv("log_dca_sim.csv")
        # (sym, t1_open_ts) 키로 그룹핑하여 각 T1 포지션의 시계열 추적
        # 가상 DCA 트리거 시뮬 (예: T2 -1.0%, T3 -2.0%)
        # 가상 평단/TP1/HARD_SL 계산
        # PTP drop 0.4 시뮬: balance peak 추적 → drop 도달 시점 청산
        # 슬롯 한계 시뮬: active_count로 새 진입 가능성 판정
    """
    row = {
        "time": _now_str(),
        "trace_id": str(trace_id),
        "symbol": symbol,
        "side": side,
        "t1_ep": round(float(t1_ep), 8),
        "t1_open_ts": round(float(t1_open_ts), 1),
        "t1_amt": round(float(t1_amt), 8),
        "mark_price": round(float(mark_price), 8),
        "t1_roi_pct": round(float(t1_roi_pct), 4),
        "actual_tier": int(actual_tier),
        "actual_blended_ep": round(float(actual_blended_ep), 8),
        "actual_amt": round(float(actual_amt), 8),
        "balance": round(float(balance), 2),
        "active_count": int(active_count),
    }
    _append_csv(_log_path("log_dca_sim.csv"), DCA_SIM_COLUMNS, row)
