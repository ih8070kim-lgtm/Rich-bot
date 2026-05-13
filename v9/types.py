"""
V9 Trinity Types
공통 데이터 타입 / 열거형 정의
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── Intent 타입 ─────────────────────────────────────────────────
class IntentType(str, Enum):
    FORCE_CLOSE   = "FORCE_CLOSE"
    CLOSE         = "CLOSE"
    TP1           = "TP1"
    TP2           = "TP2"           # v9.6: TP1 재사용 분리
    TRAIL_ON      = "TRAIL_ON"
    DCA           = "DCA"
    OPEN          = "OPEN"


# ── Intent 우선순위 (낮을수록 먼저) ────────────────────────────
INTENT_PRIORITY = {
    IntentType.FORCE_CLOSE: 0,
    IntentType.CLOSE:       1,
    IntentType.TP1:         2,
    IntentType.TP2:         2,
    IntentType.TRAIL_ON:    3,
    IntentType.DCA:         4,
    IntentType.OPEN:        5,
}


# ── Reject 코드 ─────────────────────────────────────────────────
class RejectCode(str, Enum):
    REJECT_INVALID_SNAPSHOT       = "REJECT_INVALID_SNAPSHOT"
    REJECT_DD_SHUTDOWN_ACTIVE     = "REJECT_DD_SHUTDOWN_ACTIVE"
    FORCE_DD_HARDCUT              = "FORCE_DD_HARDCUT"
    REJECT_KILLSWITCH_BLOCK_NEW   = "REJECT_KILLSWITCH_BLOCK_NEW"
    REJECT_KILLSWITCH_BLOCK_DCA   = "REJECT_KILLSWITCH_BLOCK_DCA"
    REJECT_TOGGLE_OFF             = "REJECT_TOGGLE_OFF"
    REJECT_SLOT_LIMIT             = "REJECT_SLOT_LIMIT"
    REJECT_LONG_SLOT_LIMIT        = "REJECT_LONG_SLOT_LIMIT"
    REJECT_SHORT_SLOT_LIMIT       = "REJECT_SHORT_SLOT_LIMIT"
    FORCE_T4_MAXLOSS_BREACH       = "FORCE_T4_MAXLOSS_BREACH"
    REJECT_COOLDOWN               = "REJECT_COOLDOWN"
    REJECT_CORR_LOW               = "REJECT_CORR_LOW"
    REJECT_EXPOSURE_CAP           = "REJECT_EXPOSURE_CAP"       # v9.8: 방향별 총 노출 캡
    REJECT_ASYM_COVER_RATIO       = "REJECT_ASYM_COVER_RATIO"   # v9.8: ASYM 커버비율 초과
    APPROVED                      = "APPROVED"


# ── 레짐 ────────────────────────────────────────────────────────
class Regime(str, Enum):
    LOW  = "LOW"
    MID  = "MID"
    HIGH = "HIGH"


# ── 포지션 상태 ─────────────────────────────────────────────────
@dataclass
class PositionState:
    symbol: str
    side: str                       # 'buy' | 'sell'
    ep: float                       # 평균 진입가
    amt: float                      # 수량
    time: float                     # 진입 시각 (unix)
    last_dca_time: float
    atr: float
    tag: str
    step: int = 0                   # 0=진입, 1=TP1 이후 trailing, 2=TP2 이후
    dca_level: int = 1              # 1~4
    dca_targets: list[dict] = field(default_factory=list)
    max_roi_seen: float = 0.0
    pending_dca: dict | None = None
    tp1_price: float | None = None
    trailing_on_time: float | None = None  # trailing 시작 시각
    hedge_mode: bool = False
    seed_stage: int = 0
    hedge_signal: dict | None = None
    exit_focus: bool = False


# ── 슬롯 요약 ───────────────────────────────────────────────────
@dataclass
class SlotCounts:
    total: int = 0
    long: int = 0
    short: int = 0
    # RISK_SLOTS: step>=1 제외 버전
    risk_total: int = 0
    risk_long: int = 0
    risk_short: int = 0


# ── 마켓 스냅샷 ─────────────────────────────────────────────────
@dataclass
class MarketSnapshot:
    tickers: dict
    all_prices: dict
    all_volumes: dict
    ohlcv_pool: dict
    correlations: dict
    btc_price: float
    btc_1h_change: float
    btc_6h_change: float
    btc_10m_change: float  # ★ V14.17: 10분 변화율 (default 제거 — dataclass 순서)
    dev_ma: dict
    real_balance_usdt: float
    free_balance_usdt: float
    margin_ratio: float
    baseline_balance: float
    global_targets_long: list
    global_targets_short: list
    timestamp: float
    valid: bool
    all_fundings: dict = field(default_factory=dict)
    # ★ V10.31q: universe 선정 시 계산된 심볼별 beta (TREND_NOSLOT 로그용)
    beta_by_sym: dict = field(default_factory=dict)
    # ★ V10.31AM: 3시간 correlation (진입 필터 전용, 단기 decoupling 감지)
    correlations_3h: dict = field(default_factory=dict)
    # ★ V10.31AO: 30분 correlation (진입 필터, 더 짧은 디커플링 감지 - 혼자 튀는 놈 식별)
    correlations_30m: dict = field(default_factory=dict)
    # ★ V10.31AM3 hotfix-21: 5분 변동성 비율 (alt_std / btc_std) — 로그 전용, 진입 결정 X
    #   "위아래로 튀는 알트 차단" 가설 검증용. 1주 누적 후 임계 결정
    vol_ratio_5m_by_sym: dict = field(default_factory=dict)


# ── Intent ──────────────────────────────────────────────────────
@dataclass
class Intent:
    trace_id: str
    intent_type: IntentType
    symbol: str
    side: str                       # 'buy' | 'sell'
    qty: float
    price: float | None = None   # None = market
    reason: str = ""
    reject_code: RejectCode | None = None
    approved: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ── 주문 결과 ───────────────────────────────────────────────────
@dataclass
class OrderResult:
    trace_id: str
    success: bool
    order_id: str | None
    symbol: str
    side: str
    qty: float
    avg_price: float
    filled_qty: float
    order_type: str
    tag: str
    error: str | None = None
    realized_pnl: float = 0.0  # ★ V10.31b: 바이낸스 realizedPnl
    fee_usdt: float = 0.0  # ★ V10.31d: 거래 수수료 합계 (USDT 환산)
