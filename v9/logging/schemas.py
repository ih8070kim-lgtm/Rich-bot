"""
V9 Logging Schemas
CSV 로그 컬럼 정의 (6종)
"""

# ── log_intents ─────────────────────────────────────────────────
INTENTS_COLUMNS = [
    "time", "trace_id", "intent_type", "symbol", "side",
    "qty", "price", "reason", "approved", "reject_code",
    "role", "source_sym",
]

# ── log_risk ────────────────────────────────────────────────────
RISK_COLUMNS = [
    "time", "trace_id", "symbol", "intent_type",
    "reject_code", "margin_ratio", "risk_slots_total",
    "risk_slots_long", "risk_slots_short",
    "step", "dca_level", "note"
]

# ── log_orders ──────────────────────────────────────────────────
ORDERS_COLUMNS = [
    "time", "trace_id", "symbol", "side", "order_type",
    "qty", "price", "tag", "order_id", "status"
]

# ── log_fills ───────────────────────────────────────────────────
FILLS_COLUMNS = [
    "time", "trace_id", "symbol", "side",
    "avg_price", "filled_qty", "notional", "tag", "order_id",
    # ★ 청산 체결 시 추가 (OPEN은 빈값)
    "ep", "pnl_usdt", "roi_pct", "dca_level", "hold_sec",
]

# ── log_positions ───────────────────────────────────────────────
POSITIONS_COLUMNS = [
    "time", "trace_id", "symbol", "side", "ep", "amt",
    "dca_level", "step", "roi_pct", "max_roi_seen",
    "trailing_on", "hedge_mode", "tag",
    # ★ 추가 필드
    "curr_price", "notional", "unrealized_pnl",
    # ★ v10.2: role 분류
    "role", "source_sym",
]

# ── log_trades (신규) ────────────────────────────────────────────
# OPEN→CLOSE 완성 거래 1건 = 1행. 분석의 핵심 로그
TRADES_COLUMNS = [
    "time",          # 청산 시각
    "trace_id",      # 청산 trace_id
    "symbol",
    "side",          # buy/sell (OPEN 기준)
    "ep",            # 평단
    "exit_price",    # 청산가
    "amt",           # 청산 수량
    "pnl_usdt",      # 실현 손익 ($)
    "roi_pct",       # 레버리지 포함 ROI%
    "dca_level",     # 최종 DCA 레벨 (T1~T4)
    "hold_sec",      # 보유 시간 (초)
    "reason",        # TRAIL_ON / TP1 / CLOSE / FORCE_CLOSE
    "hedge_mode",    # 헷지 포지션 여부
    "was_hedge",     # 헷지→노말 전환 후 청산 여부
    "max_roi_seen",  # 보유 중 최대 ROI
    "entry_type",    # ★ v9.9: MR | PULLBACK | ASYM
    # ★ v10.2: role 분류
    "role",          # CORE_MR | CORE_PULLBACK | BALANCE | HEDGE
    "source_sym",    # HEDGE일 때 소스 심볼
]

# ── log_universe ────────────────────────────────────────────────
UNIVERSE_COLUMNS = [
    "time", "trace_id", "top10", "long_4", "short_4",
    "regime", "btc_price", "note"
]
