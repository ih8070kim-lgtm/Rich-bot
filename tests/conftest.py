"""
테스트 하네스 — pytest fixture 정의
====================================
거래소/네트워크 없이 핵심 로직을 테스트하기 위한 가짜 객체 팩토리.
"""

import time
import pytest
from v9.types import MarketSnapshot


# ─── 헬퍼 ────────────────────────────────────────────────────────


def _pos(ep: float, amt: float, side: str, role: str = "CORE_MR", dca_level: int = 1) -> dict:
    """포지션 dict 생성 헬퍼."""
    return {
        "ep": ep,
        "amt": amt,
        "side": side,
        "role": role,
        "dca_level": dca_level,
        "max_roi_seen": 0.0,
        "step": 0,
        "hedge_mode": False,
        "was_hedge": False,
        "entry_type": "MR",
        "tag": "",
        "source_sym": "",
    }


def _sym_st(p_long=None, p_short=None) -> dict:
    """심볼 state dict 생성 헬퍼."""
    return {"p_long": p_long, "p_short": p_short}


def _snapshot(
    real_balance_usdt: float = 10_000.0,
    margin_ratio: float = 0.10,
    all_prices: dict = None,
) -> MarketSnapshot:
    """최소한의 MarketSnapshot 생성 헬퍼."""
    return MarketSnapshot(
        tickers={},
        all_prices=all_prices or {},
        all_volumes={},
        ohlcv_pool={},
        correlations={},
        btc_price=80_000.0,
        btc_1h_change=0.0,
        btc_6h_change=0.0,
        dev_ma={},
        real_balance_usdt=real_balance_usdt,
        free_balance_usdt=real_balance_usdt * 0.5,
        margin_ratio=margin_ratio,
        baseline_balance=real_balance_usdt,
        global_targets_long=[],
        global_targets_short=[],
        timestamp=time.time(),
        valid=True,
        all_fundings={},
    )


# ─── Fixtures ────────────────────────────────────────────────────


@pytest.fixture
def snap():
    """기본 스냅샷 (잔고 10,000 USDT, MR 10%)."""
    return _snapshot()


@pytest.fixture
def snap_factory():
    """커스텀 스냅샷 생성 팩토리."""
    return _snapshot


@pytest.fixture
def pos():
    """포지션 dict 생성 팩토리."""
    return _pos


@pytest.fixture
def sym_st():
    """심볼 state dict 생성 팩토리."""
    return _sym_st


@pytest.fixture
def balanced_st():
    """스큐 없는 균형 상태: long 500 USDT 마진 = short 500 USDT 마진 (total_cap=10,000)."""
    # ep=100, amt=150, side=buy  → margin = 150*100/3 = 5,000  → ratio = 5000/10000 = 0.5
    # ep=100, amt=150, side=sell → margin = 150*100/3 = 5,000  → ratio = 5000/10000 = 0.5
    return {
        "ETH/USDT": _sym_st(
            p_long=_pos(ep=100.0, amt=150.0, side="buy"),
            p_short=_pos(ep=100.0, amt=150.0, side="sell"),
        )
    }


@pytest.fixture
def long_heavy_st():
    """롱 heavy 상태: long 마진 비율 0.30, short 마진 비율 0.10 → skew=0.20."""
    # long:  ep=100, amt=450  → 450*100/3 = 15,000 / 50,000 = 0.30
    # short: ep=100, amt=150  → 150*100/3 =  5,000 / 50,000 = 0.10
    total_cap = 50_000.0
    snap = _snapshot(real_balance_usdt=total_cap)
    st = {
        "ETH/USDT": _sym_st(
            p_long=_pos(ep=100.0, amt=450.0, side="buy"),
            p_short=_pos(ep=100.0, amt=150.0, side="sell"),
        )
    }
    return st, snap, total_cap


@pytest.fixture
def short_heavy_st():
    """숏 heavy 상태: short 마진 비율 0.30, long 마진 비율 0.10 → skew=0.20."""
    total_cap = 50_000.0
    snap = _snapshot(real_balance_usdt=total_cap)
    st = {
        "ETH/USDT": _sym_st(
            p_long=_pos(ep=100.0, amt=150.0, side="buy"),
            p_short=_pos(ep=100.0, amt=450.0, side="sell"),
        )
    }
    return st, snap, total_cap
