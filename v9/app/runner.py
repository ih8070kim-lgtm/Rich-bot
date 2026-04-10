"""
V9 App Runner  (v10.27)
메인 루프: 스냅샷 → Intent 생성 → 리스크 평가 → 실행 → 포지션 북 갱신
"""
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime

import ccxt
from dotenv import load_dotenv

from v9.config import (
    ACTIVATION_THRESHOLD, HEARTBEAT_FILE,
    DD_SHUTDOWN_THRESHOLD, DD_SHUTDOWN_HOURS,
    LEVERAGE, FEE_RATE,
    SYM_MIN_QTY, SYM_MIN_QTY_DEFAULT,
)
from v9.types import MarketSnapshot, IntentType
from v9.datafeed.market_snapshot import fetch_market_snapshot
from v9.datafeed.universe_asym_v2 import update_universe
from v9.strategy.planners import generate_all_intents
from v9.strategy.strategy_core import apply_order_results, snapshot_positions
from v9.risk.risk_manager import evaluate_intent
from v9.execution.execution_engine import execute_intents
from v9.execution.position_book import (
    load_position_book, save_position_book, ensure_slot, clear_position,
    get_p, set_p, iter_positions, is_active, get_pending_entry, set_pending_entry,
    load_minroi, save_minroi, update_minroi,
)
from v9.risk.slot_manager import count_slots
from v9.logging.logger_csv import log_risk
from v9.utils.utils_time import now_str, today_str
# [BUG-5 FIX] 체결 알림 연결
try:
    import sys as _sys, os as _os
    _tg_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    _sys.path.insert(0, _tg_root)
    from telegram_engine import notify_fill as _notify_fill
    from telegram_engine import notify_async_fill as _notify_async_fill
    _TELEGRAM_OK = True
except Exception as _tg_err:
    print(f"[V9 Runner] telegram_engine import 실패 (알림 비활성): {_tg_err}")
    _TELEGRAM_OK = False
from v9.risk.risk_manager import generate_corrguard_intents

# ★ Beta Cycle 엔진
try:
    from v9.config import BC_ENABLED as _BC_ENABLED
    if _BC_ENABLED:
        from v9.engines.beta_cycle import bc_init, bc_on_daily_close, bc_on_tick
    else:
        _BC_ENABLED = False
except Exception as _bc_err:
    print(f"[V9 Runner] Beta Cycle import 실패(비활성): {_bc_err}")
    _BC_ENABLED = False

# ★ Crash Bounce 엔진
try:
    from v9.config import CB_ENABLED as _CB_ENABLED
    if _CB_ENABLED:
        from v9.engines.crash_bounce import cb_init, cb_on_tick
    else:
        _CB_ENABLED = False
except Exception as _cb_err:
    print(f"[V9 Runner] Crash Bounce import 실패(비활성): {_cb_err}")
    _CB_ENABLED = False


# ═══════════════════════════════════════════════════════════════
# v10.11b: 바이낸스 ↔ 포지션북 동기화
# DCA 체결이 포지션북에 미반영되는 버그 방어
# ═══════════════════════════════════════════════════════════════
_last_sync_ts = 0.0
_SYNC_INTERVAL = 30  # 초

async def _sync_positions_with_exchange(ex, st, snapshot=None):
    """바이낸스 실제 포지션과 포지션북 비교, 불일치 시 바이낸스 기준 반영.
    ★ v10.14: snapshot 파라미터 추가 (dca_level 역추정 정확도 개선)
    """
    global _last_sync_ts
    now = time.time()
    if now - _last_sync_ts < _SYNC_INTERVAL:
        return
    _last_sync_ts = now

    try:
        positions = await asyncio.to_thread(ex.fetch_positions)
    except Exception as e:
        print(f"[SYNC] fetch_positions 실패(무시): {e}")
        return

    # 바이낸스 포지션을 {(symbol, side): {qty, ep}} 로 변환
    ex_pos = {}
    for pos in positions:
        contracts = float(pos.get('contracts', 0) or 0)
        if contracts <= 0:
            continue
        raw_sym = pos.get('symbol', '')  # "INJ/USDT:USDT"
        sym = raw_sym.replace(':USDT', '') if ':USDT' in raw_sym else raw_sym
        side_raw = pos.get('side', '')  # "long" or "short"
        side = "buy" if side_raw == "long" else "sell"
        ep = float(pos.get('entryPrice', 0) or 0)
        ex_pos[(sym, side)] = {'qty': contracts, 'ep': ep}

    # 포지션북의 모든 활성 포지션 수집
    book_pos = {}
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for side, p in iter_positions(sym_st):
            if isinstance(p, dict):
                qty = float(p.get('amt', 0) or 0)
                if qty > 0:
                    book_pos[(sym, side)] = p

    # ── 1) 바이낸스에 있는데 포지션북 qty/ep 불일치 → 바이낸스 기준 수정 ──
    for (sym, side), ex_info in ex_pos.items():
        ex_qty = ex_info['qty']
        ex_ep = ex_info['ep']
        book_p = book_pos.get((sym, side))

        if book_p:
            book_qty = float(book_p.get('amt', 0) or 0)
            book_ep = float(book_p.get('ep', 0) or 0)
            qty_diff = abs(ex_qty - book_qty) / book_qty > 0.05 if book_qty > 0 else False
            ep_diff = abs(ex_ep - book_ep) / book_ep > 0.001 if book_ep > 0 and ex_ep > 0 else False

            if qty_diff or ep_diff:
                old_qty = book_qty
                old_ep = book_ep
                if qty_diff:
                    book_p['amt'] = ex_qty
                    # ★ V10.28b FIX: qty 증가 시 dca_level 역추정 제거
                    # 이유: notional 기반 추정이 balance 변동에 취약 → T2를 T4로 오추정
                    # DCA 체결은 _manage_pending_limits → _apply_pending_fill에서 정확하게 처리됨
                    # 역추정은 RECOVERED 포지션(고아 복구)에서만 사용
                    if ex_qty > book_qty * 1.05:
                        print(f"[SYNC] {sym} {side} qty 증가 감지 "
                              f"({book_qty:.1f}→{ex_qty:.1f}) — dca_level 유지 T{int(book_p.get('dca_level', 1))}")
                if ep_diff and ex_ep > 0:
                    book_p['ep'] = ex_ep
                _what = []
                if qty_diff: _what.append(f"qty:{old_qty:.1f}→{ex_qty:.1f}")
                if ep_diff:  _what.append(f"ep:{old_ep:.6f}→{ex_ep:.6f}")
                print(f"[SYNC] ★ {sym} {side} 수정: {' | '.join(_what)}")
        else:
            # ★ v10.15c: pending_limit 메타데이터가 있으면 role/dca 반영
            _pl_role = "CORE_MR"
            _pl_dca = None  # None이면 역추정 사용
            _pl_entry_type = "MR"
            try:
                from v9.execution.order_router import get_pending_limits
                for _pl_oid, _pl_info in get_pending_limits().items():
                    if _pl_info.get("sym") == sym and _pl_info.get("side") == side:
                        _pl_role = _pl_info.get("role", "") or "CORE_MR"
                        _pl_entry_type = _pl_info.get("entry_type", "") or "MR"
                        _pl_dca_raw = _pl_info.get("dca_level", 1)
                        if _pl_dca_raw and int(_pl_dca_raw) > 1:
                            _pl_dca = int(_pl_dca_raw)
                        print(f"[SYNC] ★ {sym} {side} pending_limit 메타 반영: "
                              f"role={_pl_role} dca={_pl_dca} entry={_pl_entry_type}")
                        break
            except Exception:
                pass

            # ★ v10.11b: 포지션북에 없는데 바이낸스에 있음 → 자동 복구
            print(f"[SYNC] ★ {sym} {side} 고아 포지션 복구: "
                  f"qty={ex_qty:.1f} ep={ex_ep:.4f} (바이낸스 기준)")
            ensure_slot(st, sym)
            sym_st = st[sym]
            # ★ v10.15: 노셔널 기반 dca_level 역추정 (T1=1 고정 대신)
            _rv_notional = ex_qty * ex_ep if ex_ep > 0 else 0
            _rv_dca = 1
            if _rv_notional > 0 and snapshot is not None:
                from v9.config import DCA_WEIGHTS as _DW, LEVERAGE as _LV, GRID_DIVISOR as _TS
                _rv_bal = float(getattr(snapshot, 'real_balance_usdt', 4000) or 4000)
                _rv_grid = (_rv_bal / _TS) * _LV
                _rv_tw = sum(_DW)
                _rv_cum = 0
                for _wi in range(len(_DW)):
                    _rv_cum += _DW[_wi] / _rv_tw
                    if _rv_notional <= _rv_grid * _rv_cum * 1.15:
                        _rv_dca = _wi + 1; break
                    _rv_dca = _wi + 1
                _rv_dca = min(_rv_dca, 5)
            # ★ v10.15c: pending_limit dca가 있으면 역추정 대신 사용
            _final_dca = _rv_dca
            if _pl_dca is not None and _pl_dca > _rv_dca:
                _final_dca = _pl_dca
                print(f"[SYNC] ★ {sym} {side} pending_limit dca={_pl_dca} 적용 (역추정={_rv_dca})")
            elif _rv_dca > 1:
                print(f"[SYNC] ★ {sym} {side} RECOVERED dca_level 역추정: {_rv_dca} "
                      f"(notional=${_rv_notional:.0f} grid=${_rv_grid:.0f})")
            # ★ v10.24 Fix A: RECOVERED 포지션 role을 무조건 CORE_MR로 강제
            # pending_limit에서 CORE_BREAKOUT을 가져오면 MR 슬롯 카운트에서 빠져
            # 좀비 슬롯이 되는 근본 원인 차단
            _pl_role = "CORE_MR"
            _pl_entry_type = "MR"
            set_p(sym_st, side, {
                "symbol": sym, "side": side,
                "ep": ex_ep, "original_ep": ex_ep,
                "amt": ex_qty,
                "time": now, "last_dca_time": 0,
                "atr": 0.0, "tag": "V9_RECOVERED",
                "step": 0, "dca_level": _final_dca,
                "dca_targets": [],
                "max_roi_seen": 0.0, "worst_roi": 0.0, "pending_dca": None,
                "trailing_on_time": None,
                "hedge_mode": False,
                "tp1_done": False, "tp2_done": False,
                "entry_type": _pl_entry_type, "role": _pl_role,
                "source_sym": "", "asym_forced": False,
                "locked_regime": "LOW",
                "hedge_entry_price": 0.0,
                "t5_entry_price": 0.0,
                "insurance_timecut": 0,
            })
            # ★ v10.15: T5 복구 시 max_dca_reached 세팅
            if _final_dca >= 5:
                _rv_p = get_p(sym_st, side)
                if _rv_p:
                    _rv_p["max_dca_reached"] = True
            # ★ v10.14b: 복구 시 잔류 pending_entry 해제 (유령 슬롯 방지)
            from v9.execution.position_book import set_pending_entry as _spe_sync
            _spe_sync(sym_st, side, None)

    # ── 2) 포지션북에 있는데 바이낸스에 없음 → 유령 포지션 제거 ──
    # ★ v10.17: 첫 sync(재시작 직후)는 "다운타임 중 청산" 가능성 → 상세 로그
    for (sym, side), book_p in book_pos.items():
        if (sym, side) not in ex_pos:
            book_qty = float(book_p.get('amt', 0) or 0)
            if book_qty > 0:
                _ep = float(book_p.get('ep', 0) or 0)
                _dca = int(book_p.get('dca_level', 1) or 1)
                _role = book_p.get('role', '')
                _entry_type = book_p.get('entry_type', 'MR')
                print(f"[SYNC] ★ {sym} {side} 유령 포지션 제거: "
                      f"qty={book_qty:.1f} ep={_ep:.4f} "
                      f"dca={_dca} role={_role} (바이낸스에 없음)")
                # ★ v10.18: 유령 제거 시 log_trade 기록 (감사 추적)
                try:
                    from v9.logging.logger_csv import log_trade as _lt_ghost
                    _lt_ghost(
                        trace_id=str(uuid.uuid4())[:8],
                        symbol=sym,
                        side=side,
                        ep=_ep,
                        exit_price=0.0,  # 알 수 없음
                        amt=book_qty,
                        pnl_usdt=0.0,    # 알 수 없음
                        roi_pct=0.0,
                        dca_level=_dca,
                        hold_sec=0.0,
                        reason="GHOST_CLEANUP",
                        hedge_mode=bool(book_p.get('hedge_mode', False)),
                        was_hedge=bool(book_p.get('was_hedge', False)),
                        max_roi_seen=float(book_p.get('max_roi_seen', 0) or 0),
                        entry_type=str(_entry_type),
                        role=str(_role),
                        source_sym=str(book_p.get('source_sym', '') or ''),
                    )
                except Exception as _lt_e:
                    print(f"[SYNC] log_trade(GHOST) 실패(무시): {_lt_e}")
                sym_st = st.get(sym, {})
                set_p(sym_st, side, None)


# ═══════════════════════════════════════════════════════════════
# ★ V10.27e: save 래퍼 — 글로벌 state 영속화 포함
# ═══════════════════════════════════════════════════════════════
def _save_all(st, cooldowns, system_state):
    """save_position_book + 글로벌 전략/헷지 state 동기화."""
    try:
        from v9.strategy.planners import save_strategy_state
        from v9.engines.hedge_core import save_hedge_state
        save_strategy_state(system_state)
        save_hedge_state(system_state)
    except Exception as _e:
        print(f"[_save_all] global state save 실패(무시): {_e}")
    # ★ V10.29c: BC/CB state 영속화
    try:
        from v9.engines.beta_cycle import bc_save_state
        from v9.engines.crash_bounce import cb_save_state
        bc_save_state(system_state)
        cb_save_state(system_state)
    except Exception as _e:
        print(f"[_save_all] BC/CB state save 실패(무시): {_e}")
    save_position_book(st, cooldowns, system_state)


# ═══════════════════════════════════════════════════════════════
# ★ 다운타임 중 청산 감지 (v10.18)
# 봇 재시작 시 _last_save_ts ~ 현재 사이에 청산된 포지션을 감지하고
# 텔레그램으로 알림 발송
# ═══════════════════════════════════════════════════════════════

async def _check_downtime_trades(ex, st, system_state):
    """
    재시작 시 다운타임 중 청산된 포지션을 감지하여 텔레그램 알림 발송.
    ① _last_save_ts 확인 → 다운타임 1분 미만이면 스킵
    ② 포지션북의 활성 포지션 목록 수집
    ③ fetch_positions()로 현재 바이낸스 포지션 조회
    ④ 차집합 (북에 있음 + 거래소에 없음) = 다운타임 중 청산된 포지션
    ⑤ fetch_my_trades(sym, since=last_save_ts_ms)로 청산가 + realizedPnl 조회
    ⑥ 텔레그램 ⚠️ 다운타임 청산 알림 발송
    """
    last_save = system_state.get('_last_save_ts', 0)
    now = time.time()
    downtime_sec = now - last_save if last_save > 0 else 0

    # 다운타임 1분 미만이면 스킵 (정상 재시작)
    if downtime_sec < 60:
        print(f"[DOWNTIME] 다운타임 {downtime_sec:.0f}초 — 스킵")
        return

    downtime_min = downtime_sec / 60
    print(f"[DOWNTIME] ★ 다운타임 감지: {downtime_min:.1f}분 "
          f"(마지막 저장: {datetime.fromtimestamp(last_save).strftime('%H:%M:%S')})")

    # ② 포지션북의 활성 포지션 수집
    book_positions = {}  # {(sym, side): p_dict}
    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for side, p in iter_positions(sym_st):
            if isinstance(p, dict) and float(p.get('amt', 0) or 0) > 0:
                book_positions[(sym, side)] = p

    if not book_positions:
        print(f"[DOWNTIME] 활성 포지션 없음 — 스킵")
        return

    # ③ 현재 바이낸스 포지션 조회
    try:
        positions = await asyncio.to_thread(ex.fetch_positions)
    except Exception as e:
        print(f"[DOWNTIME] fetch_positions 실패: {e}")
        return

    ex_pos = set()
    for pos in positions:
        contracts = float(pos.get('contracts', 0) or 0)
        if contracts <= 0:
            continue
        raw_sym = pos.get('symbol', '')
        sym = raw_sym.replace(':USDT', '') if ':USDT' in raw_sym else raw_sym
        side_raw = pos.get('side', '')
        side = "buy" if side_raw == "long" else "sell"
        ex_pos.add((sym, side))

    # ④ 차집합: 포지션북에 있지만 거래소에 없음 = 다운타임 중 청산
    closed_during_downtime = {}
    for (sym, side), p in book_positions.items():
        if (sym, side) not in ex_pos:
            closed_during_downtime[(sym, side)] = p

    if not closed_during_downtime:
        print(f"[DOWNTIME] 다운타임 중 청산된 포지션 없음")
        return

    print(f"[DOWNTIME] ★ 다운타임 중 청산 감지: "
          f"{[f'{s[0]} {s[1]}' for s in closed_during_downtime.keys()]}")

    # ⑤ fetch_my_trades로 청산 거래 이력 조회
    last_save_ms = int(last_save * 1000)
    alerts = []

    for (sym, side), p in closed_during_downtime.items():
        entry_price = float(p.get('ep', 0) or 0)
        entry_amt = float(p.get('amt', 0) or 0)
        dca_level = p.get('dca_level', 0)
        role = p.get('role', '')
        side_label = "LONG" if side == "buy" else "SHORT"

        # 거래 이력 조회
        realized_pnl = 0.0
        close_price = 0.0
        close_time_str = "알 수 없음"

        try:
            trades = await asyncio.to_thread(
                ex.fetch_my_trades, sym, since=last_save_ms, limit=500
            )
            # 해당 방향의 청산 거래만 필터
            # 롱 청산 = sell 거래, 숏 청산 = buy 거래
            close_side = "sell" if side == "buy" else "buy"
            close_trades = [
                t for t in trades
                if t.get('side') == close_side
            ]
            if close_trades:
                # 마지막 청산 거래 기준
                last_trade = close_trades[-1]
                close_price = float(last_trade.get('price', 0) or 0)
                close_ts = last_trade.get('timestamp', 0)
                if close_ts:
                    close_time_str = datetime.fromtimestamp(
                        close_ts / 1000
                    ).strftime('%m/%d %H:%M:%S')
                # realizedPnl 합산
                for t in close_trades:
                    info = t.get('info', {})
                    rpnl = float(info.get('realizedPnl', 0) or 0)
                    realized_pnl += rpnl
        except Exception as e:
            print(f"[DOWNTIME] {sym} fetch_my_trades 실패: {e}")

        # ROI 계산
        roi = 0.0
        if entry_price > 0 and close_price > 0:
            if side == "buy":
                roi = (close_price - entry_price) / entry_price * 100
            else:
                roi = (entry_price - close_price) / entry_price * 100

        # 알림 메시지 구성
        pnl_sign = "+" if realized_pnl >= 0 else ""
        roi_sign = "+" if roi >= 0 else ""
        emoji = "✅" if realized_pnl >= 0 else "🔴"

        alert_msg = (
            f"  {emoji} <b>{sym}</b> {side_label}"
            f" (DCA{dca_level}"
            f"{' ' + role if role else ''})\n"
            f"    진입가: {entry_price:.4f}"
            f" → 청산가: {close_price:.4f}\n"
            f"    ROI: {roi_sign}{roi:.2f}%"
            f" | PnL: {pnl_sign}${realized_pnl:.2f}\n"
            f"    청산 시각: {close_time_str}"
        )
        alerts.append(alert_msg)

        # ★ log_trades.csv에 기록 (감사 추적)
        try:
            from v9.logging.logger_csv import log_trade as _lt_dt
            _hold = now - float(p.get('time', now) or now)
            _lt_dt(
                trace_id=str(uuid.uuid4())[:8],
                symbol=sym,
                side=side,
                ep=entry_price,
                exit_price=close_price,
                amt=entry_amt,
                pnl_usdt=realized_pnl,
                roi_pct=roi,
                dca_level=int(dca_level),
                hold_sec=_hold if _hold > 0 else 0.0,
                reason="DOWNTIME_CLOSE",
                hedge_mode=bool(p.get('hedge_mode', False)),
                was_hedge=bool(p.get('was_hedge', False)),
                max_roi_seen=float(p.get('max_roi_seen', 0) or 0),
                entry_type=str(p.get('entry_type', 'MR') or 'MR'),
                role=str(role),
                source_sym=str(p.get('source_sym', '') or ''),
            )
        except Exception as _lt_e:
            print(f"[DOWNTIME] log_trade 실패(무시): {_lt_e}")

        print(f"[DOWNTIME] {sym} {side_label}: "
              f"ep={entry_price:.4f} → cp={close_price:.4f} "
              f"ROI={roi:+.2f}% PnL=${realized_pnl:+.2f}")

        await asyncio.sleep(0.1)  # rate limit 방지

    # ⑥ 텔레그램 알림 발송
    if alerts and _TELEGRAM_OK:
        header = (
            f"⚠️ <b>다운타임 청산 감지</b>\n"
            f"다운타임: {downtime_min:.0f}분 "
            f"({datetime.fromtimestamp(last_save).strftime('%H:%M')} "
            f"→ {datetime.fromtimestamp(now).strftime('%H:%M')})\n"
            f"청산 {len(alerts)}건:\n"
        )
        msg = header + "\n".join(alerts)
        try:
            from telegram_engine import send_telegram_message
            await send_telegram_message(msg)
            print(f"[DOWNTIME] ★ 텔레그램 알림 발송 완료 ({len(alerts)}건)")
        except Exception as e:
            print(f"[DOWNTIME] 텔레그램 발송 실패: {e}")


# ── 경로 상수 ────────────────────────────────────────────────────
_BASE_DIR    = os.path.dirname(os.path.abspath(__file__))   # v9/app
_PROJECT_DIR = os.path.abspath(os.path.join(_BASE_DIR, "..", ".."))  # 프로젝트 루트


# ── 호환 JSON writer ─────────────────────────────────────────────
def _write_json_atomic(path: str, obj: dict):
    """원자적 JSON 파일 쓰기 (Windows 잠김 방어 포함)"""
    tmp = path + ".tmp"
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    try:
        os.replace(tmp, path)
    except PermissionError:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass  # tmp 삭제 실패 무시
    except Exception as _log_e:
        print(f"[V9 Runner] 로그 순환 오류(무시): {_log_e}")


def _get_pending_limits_count() -> int:
    """pending limit 주문 수 조회 (system_state.json용)"""
    try:
        from v9.execution.order_router import get_pending_limits
        return len(get_pending_limits())
    except Exception:
        return 0


def _write_system_state_compat(snapshot: "MarketSnapshot", system_state: dict, st: dict):
    """
    텔레그램 봇이 읽는 system_state.json 최소 필드를 프로젝트 루트에 저장.
    st: position_book의 st dict (symbol → slot dict)
    """
    try:
        positions = []
        for sym, slot in st.items():
            for pos_side, p in iter_positions(slot):
                if p is None:
                    continue
                # ★ pos_side는 iter_positions가 p_long/p_short 키에서 결정한 값
                # dict 내부 side 필드 대신 이것을 사용 (소스 오염 방어)
                side_raw = pos_side

                if hasattr(p, "dca_level"):
                    dca_level  = int(getattr(p, "dca_level", 1) or 1)
                    ep         = float(getattr(p, "ep", 0.0) or 0.0)
                    amt        = float(getattr(p, "amt", 0.0) or 0.0)
                    step       = int(getattr(p, "step", 0) or 0)
                    hedge_mode = bool(getattr(p, "hedge_mode", False))
                else:
                    dca_level  = int(p.get("dca_level", 1) or 1)
                    ep         = float(p.get("ep", 0.0) or 0.0)
                    amt        = float(p.get("amt", 0.0) or 0.0)
                    step       = int(p.get("step", 0) or 0)
                    hedge_mode = bool(p.get("hedge_mode", False))

                # ROI 계산 (레버리지 반영)
                cur_price = (snapshot.all_prices or {}).get(sym, ep) if snapshot else ep
                if ep > 0 and cur_price > 0:
                    from v9.config import LEVERAGE as _LEV
                    if side_raw == "buy":
                        roi_pct = (cur_price - ep) / ep * _LEV * 100.0
                    else:
                        roi_pct = (ep - cur_price) / ep * _LEV * 100.0
                else:
                    roi_pct = 0.0

                positions.append({
                    "symbol":       sym,
                    "side":         "BUY" if side_raw == "buy" else "SELL",
                    "tier":         dca_level,
                    "roi_pct":      round(roi_pct, 4),
                    "ep":           ep,
                    "amt":          amt,
                    "step":         step,
                    "hedge_mode":   hedge_mode,
                    "role":         str(p.get("role", "") if isinstance(p, dict) else ""),
                    "source_sym":   str(p.get("source_sym", "") if isinstance(p, dict) else ""),
                    "entry_type":   str(p.get("entry_type", "MR") if isinstance(p, dict) else "MR"),
                    # ★ v10.24: tag 필드 추가 (RECOVERED 마커용)
                    "tag":          str(p.get("tag", "") if isinstance(p, dict) else ""),
                })

        mr = float(snapshot.margin_ratio) if snapshot else 0.0
        kill_switch_on = (mr >= 0.8) or bool(system_state.get("shutdown_active", False))

        payload = {
            "updated_at":    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kill_switch_on": kill_switch_on,
            "margin_ratio":  mr,
            "total_equity":  float(snapshot.real_balance_usdt) if snapshot else 0.0,
            "free_balance":  float(snapshot.free_balance_usdt) if snapshot else 0.0,
            "positions":     positions,
            "use_long":      bool(system_state.get("use_long", True)),
            "use_short":     bool(system_state.get("use_short", True)),
            "shutdown_active": bool(system_state.get("shutdown_active", False)),
            "shutdown_reason": str(system_state.get("shutdown_reason", "")),
            "initial_balance": float(system_state.get("initial_balance", 0)),
            "baseline_balance": float(system_state.get("baseline_balance", 0)),
            # ★ v10.6: 레짐 + crash freeze
            "regime": str(system_state.get("_current_regime", "")),
            "btc_crash_freeze_until": float(system_state.get("btc_crash_freeze_until", 0)),
            # ★ v10.24: pending limits 카운트 + 슬롯 상세
            "pending_limits_count": _get_pending_limits_count(),
            "slot_mr_long": 0,
            "slot_mr_short": 0,
            "slot_total": 0,
            # ★ V10.27f: urgency 점수
            "urgency": float(system_state.get("_urgency_score", 0)),
            "heavy_avg_roi": float(system_state.get("_heavy_avg_roi", 0)),
        }

        # ★ v10.24: 슬롯 상세 정보 채우기
        try:
            from v9.risk.slot_manager import count_slots as _cs_compat
            _sc = _cs_compat(st, role_filter="CORE_MR")
            _sc_all = _cs_compat(st)
            payload["slot_mr_long"] = _sc.long
            payload["slot_mr_short"] = _sc.short
            payload["slot_total"] = _sc_all.total
        except Exception:
            pass

        path = os.path.join(_PROJECT_DIR, "system_state.json")
        _write_json_atomic(path, payload)
    except Exception as e:
        print(f"[V9 Runner] system_state.json 쓰기 실패: {e}")


def _make_exchange() -> ccxt.Exchange:
    load_dotenv("api.env")
    api_key = os.getenv("BINANCE_API_KEY")
    secret  = os.getenv("BINANCE_SECRET_KEY")
    if not api_key or not secret:
        print("[FATAL] api.env에 바이낸스 API 키가 없습니다.")
        sys.exit(1)
    return ccxt.binance({
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
        'options': {'defaultType': 'future'},
    })


# ═══════════════════════════════════════════════════════════════
# ★ V10.25: TP1 선주문 관리
# ═══════════════════════════════════════════════════════════════
_TP1_PREORDER_REPRICE_PCT = 0.003  # 0.3% 이상 차이나면 재배치


async def _cancel_tp1_preorder(ex, p: dict, sym: str):
    """기존 TP1 선주문 취소 + 레지스트리 정리."""
    oid = p.get("tp1_preorder_id")
    if not oid or oid == "DRY_PREORDER":
        p["tp1_preorder_id"] = None
        p["tp1_preorder_price"] = None
        p["tp1_preorder_ts"] = None
        return
    try:
        import asyncio as _aio
        await _aio.to_thread(ex.cancel_order, oid, sym)
        print(f"[TP1_PRE] {sym} 선주문 취소 oid={oid}")
    except Exception as _e:
        _err = str(_e)
        if "Unknown order" in _err or "-2011" in _err:
            pass  # 이미 체결 또는 만료
        else:
            print(f"[TP1_PRE] {sym} 선주문 취소 실패: {_e}")
    # 레지스트리 정리
    try:
        from v9.execution.order_router import remove_pending_limit, _PENDING_ORDERS
        remove_pending_limit(str(oid))
        _orders = _PENDING_ORDERS.get(sym, [])
        _PENDING_ORDERS[sym] = [(o, t) for o, t in _orders if o != str(oid)]
        if not _PENDING_ORDERS[sym]:
            _PENDING_ORDERS.pop(sym, None)
    except Exception:
        pass
    p["tp1_preorder_id"] = None
    p["tp1_preorder_price"] = None
    p["tp1_preorder_ts"] = None


async def _manage_tp1_preorders(ex, st, snapshot, dry_run=False):
    """TP1 목표가에 지정가 선주문 배치/갱신/취소.

    매 틱 실행. 포지션별로:
      - step=0, tp1_done=False, CORE 포지션만 대상
      - worst_roi + alpha 기반 target price 계산
      - 선주문 없으면 배치, target 변경 시 재배치, 부적격 시 취소
    """
    from v9.config import LEVERAGE, TP1_PARTIAL_RATIO, HEDGE_MODE, TP1_FIXED
    from v9.utils.utils_math import calc_roi_pct
    from v9.strategy.planners import _t3_defense, _calc_urgency

    prices = snapshot.all_prices or {}
    _urg = _calc_urgency(st, snapshot)  # ★ V10.27f

    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if not isinstance(p, dict):
                continue

            # ── 부적격 → 기존 선주문 취소 ──
            _step = int(p.get("step", 0) or 0)
            if _step != 0 or p.get("tp1_done") or p.get("pending_dca"):
                if p.get("tp1_preorder_id"):
                    await _cancel_tp1_preorder(ex, p, sym)
                continue
            _role = p.get("role", "")
            if _role in ("INSURANCE_SH", "CORE_HEDGE", "HEDGE", "SOFT_HEDGE", "BC", "CB"):
                continue
            # ★ V10.29d: URGENCY_DCA 슬롯 TP 블록
            if p.get("urgency_tp_block"):
                if _urg["urgency"] >= 12:
                    if p.get("tp1_preorder_id"):
                        await _cancel_tp1_preorder(ex, p, sym)
                    continue
                else:
                    p.pop("urgency_tp_block", None); p.pop("original_ep", None)
            # ★ V10.29: T2+ → TP1 선주문 전면 차단 (trim이 exit 담당)
            _dca_lv = int(p.get("dca_level", 1) or 1)
            if _dca_lv >= 2:
                if p.get("tp1_preorder_id"):
                    await _cancel_tp1_preorder(ex, p, sym)
                continue
            # ★ V10.28b FIX: plan_tp1 경로 limit이 pending → 선주문 불필요
            if p.get("tp1_limit_oid"):
                continue

            ep = float(p.get("ep", 0) or 0)
            curr_p = float(prices.get(sym, 0) or 0)
            if ep <= 0 or curr_p <= 0:
                continue

            dca_level = int(p.get("dca_level", 1) or 1)
            is_long = (pos_side == "buy")

            # ★ V10.29b: 최소 슬롯 유지 제거
            p.pop("min_slot_hold", None)

            # ★ V10.29b: T3 방어 블록 체크
            _def = _t3_defense(pos_side, dca_level, st, snapshot)
            if _def["blocked"]:
                if p.get("tp1_preorder_id"):
                    await _cancel_tp1_preorder(ex, p, sym)
                continue

            # TP1 target 계산
            # ★ V10.27b: T1~T2 고정값, T3~T4 worst_roi 탈출
            _worst = 0.0
            # ★ V10.29: TP1 threshold — 공유 함수 사용
            from v9.config import calc_tp1_thresh
            _worst = float(p.get("worst_roi", 0.0) or 0.0)
            _is_heavy_tp = (_urg["heavy_side"] == pos_side)
            # ★ V10.29b: T3 방어 배수 적용
            tp1_thresh = calc_tp1_thresh(dca_level, _worst, _urg["urgency"], _is_heavy_tp) * _def["tp_mult"]

            # ROI → price 변환
            if is_long:
                target_price = ep * (1.0 + tp1_thresh / (LEVERAGE * 100.0))
            else:
                target_price = ep * (1.0 - tp1_thresh / (LEVERAGE * 100.0))
            if target_price <= 0:
                continue

            # ★ V10.28b FIX: ROI >= threshold 시 선주문을 취소하지 않음
            # plan_tp1은 tp1_preorder_id가 있으면 스킵하므로 이중배치 없음.
            # 취소하면 레이스컨디션 발생: 취소 전 거래소에서 체결 + plan_tp1 새 주문 → 이중체결
            roi_now = calc_roi_pct(ep, curr_p, pos_side, LEVERAGE)
            if roi_now >= tp1_thresh:
                # 선주문이 있으면 유지 (거래소에서 자연 체결 대기)
                # 선주문이 없으면 plan_tp1이 처리 → 스킵
                continue

            # 수량 계산 — ★ V10.29d: 노셔널 기반
            from v9.config import calc_tier_notional, notional_to_qty
            total_qty = float(p.get("amt", 0) or 0)
            _tp_bal = float(getattr(snapshot, 'real_balance_usdt', 0) or 0) if snapshot else 0
            _t1_notional = calc_tier_notional(1, _tp_bal) if _tp_bal > 0 else 0
            if _t1_notional > 0 and target_price > 0:
                _t1_qty = notional_to_qty(_t1_notional, target_price)
                close_qty = _t1_qty * TP1_PARTIAL_RATIO
            else:
                close_qty = total_qty * TP1_PARTIAL_RATIO  # fallback
            # ★ V10.29d FIX: 노셔널 기반 qty가 실제 보유 초과 방지
            close_qty = min(close_qty, total_qty)
            _min_qty = SYM_MIN_QTY.get(sym, SYM_MIN_QTY_DEFAULT)
            if close_qty < _min_qty:
                close_qty = total_qty
            # ★ V10.27e: 잔량이 min_qty 미만이면 전량 청산 (RESIDUAL 0.1 무한루프 방지)
            remaining = total_qty - close_qty
            if 0 < remaining < _min_qty:
                close_qty = total_qty
            if close_qty <= 0:
                continue

            # 기존 선주문과 비교
            existing_id = p.get("tp1_preorder_id")
            existing_price = float(p.get("tp1_preorder_price", 0) or 0)
            if existing_id and existing_price > 0:
                # ★ V10.28b FIX: 10분 이상 된 선주문 → 거래소 확인 (유령 방지)
                _pre_age = time.time() - float(p.get("tp1_preorder_ts", 0) or 0)
                if _pre_age > 600:
                    try:
                        import asyncio as _aio2
                        _chk = await _aio2.to_thread(ex.fetch_order, str(existing_id), sym)
                        _chk_status = _chk.get("status", "")
                        if _chk_status in ("canceled", "expired", "rejected"):
                            print(f"[TP1_PRE] {sym} 유령 선주문 감지 (status={_chk_status}) → 재배치")
                            p["tp1_preorder_id"] = None
                            p["tp1_preorder_price"] = None
                            p["tp1_preorder_ts"] = None
                            existing_id = None
                        elif _chk_status == "closed":
                            # 이미 체결됨 — _manage_pending_limits에서 처리되지 않은 케이스
                            print(f"[TP1_PRE] {sym} 선주문 이미 체결 감지 → 클리어")
                            p["tp1_preorder_id"] = None
                            p["tp1_preorder_price"] = None
                            p["tp1_preorder_ts"] = None
                            existing_id = None
                        else:
                            # open — 갱신만
                            p["tp1_preorder_ts"] = time.time()
                    except Exception as _stale_e:
                        _stale_err = str(_stale_e)
                        if "Unknown order" in _stale_err or "-2013" in _stale_err:
                            print(f"[TP1_PRE] {sym} 유령 선주문 ({existing_id}) → 재배치")
                            p["tp1_preorder_id"] = None
                            p["tp1_preorder_price"] = None
                            p["tp1_preorder_ts"] = None
                            existing_id = None
                        else:
                            print(f"[TP1_PRE] {sym} 선주문 확인 실패: {_stale_e}")

            if existing_id and existing_price > 0:
                _diff = abs(target_price - existing_price) / existing_price
                if _diff < _TP1_PREORDER_REPRICE_PCT:
                    continue  # 0.3% 미만 차이 → 유지
                await _cancel_tp1_preorder(ex, p, sym)

            # 선주문 배치
            if dry_run:
                p["tp1_preorder_id"] = "DRY_PREORDER"
                p["tp1_preorder_price"] = target_price
                p["tp1_preorder_ts"] = time.time()
                continue
            try:
                import asyncio as _aio
                close_side = "sell" if is_long else "buy"
                safe_qty = float(ex.amount_to_precision(sym, close_qty))
                safe_price = float(ex.price_to_precision(sym, target_price))
                if safe_qty <= 0 or safe_price <= 0:
                    continue
                params = {}
                if HEDGE_MODE:
                    params["positionSide"] = "LONG" if is_long else "SHORT"
                order = await _aio.to_thread(
                    ex.create_order, sym, 'limit', close_side, safe_qty, safe_price, params=params
                )
                oid = order.get('id')
                p["tp1_preorder_id"] = oid
                p["tp1_preorder_price"] = safe_price
                p["tp1_preorder_ts"] = time.time()
                # ★ PENDING_LIMITS + PENDING_ORDERS 등록
                # → _manage_pending_limits가 체결 감지 + 텔레그램 "TP1_LIMIT" 알림
                # → cancel_pending_orders가 TRAIL_ON 전 취소 (-2022 방지)
                from v9.execution.order_router import (
                    _register_pending_limit as _rpl,
                    _register_pending as _rp,
                )
                from v9.types import Intent as _PreIntent, IntentType as _PreIT
                _pre_intent = _PreIntent(
                    trace_id=f"tp1pre_{oid}",
                    intent_type=_PreIT.TP1,
                    symbol=sym, side=close_side,
                    qty=safe_qty, price=safe_price,
                    reason="TP1_PREORDER",
                    metadata={
                        "positionSide": params.get("positionSide", ""),
                        "role": p.get("role", ""),
                        "_expected_role": p.get("role", ""),
                        "tier": 0,
                    },
                )
                _rpl(f"tp1pre_{oid}", sym, close_side, safe_qty, safe_price, oid,
                     f"V9_TP1_PRE_{sym}", _pre_intent)
                _rp(sym, oid, "TP1")
                print(f"[TP1_PRE] {sym} {pos_side} T{dca_level} 선주문 "
                      f"@{safe_price:.4f} qty={safe_qty} thresh={tp1_thresh:.1f}% "
                      f"worst={_worst:.1f}%")
            except Exception as _e:
                print(f"[TP1_PRE] {sym} 선주문 실패: {_e}")


def _trim_ohlcv_pool(snapshot) -> None:
    """
    ohlcv_pool 메모리 누수 방지.
    1m: 최대 300개, 15m: 최대 150개 유지 (planners 최대 필요: 1m 65개, 15m 15개)
    """
    pool = getattr(snapshot, 'ohlcv_pool', None)
    if not pool:
        return
    MAX_1M  = 200   # ★ V10.16: 300→200 (planners 최대 65봉 × 3배 여유)
    MAX_5M  = 80    # 5m 40봉 × 2배 여유
    MAX_15M = 150   # ★ V10.29: 일목구름 80봉 필요 → 150 유지
    MAX_1H  = 40    # 1h 20봉 × 2배 여유
    for sym in pool:
        tf_map = pool[sym]
        if not isinstance(tf_map, dict):
            continue
        if '1m'  in tf_map and len(tf_map['1m'])  > MAX_1M:
            tf_map['1m']  = tf_map['1m'][-MAX_1M:]
        if '5m'  in tf_map and len(tf_map['5m'])  > MAX_5M:
            tf_map['5m']  = tf_map['5m'][-MAX_5M:]
        if '15m' in tf_map and len(tf_map['15m']) > MAX_15M:
            tf_map['15m'] = tf_map['15m'][-MAX_15M:]
        if '1h'  in tf_map and len(tf_map['1h'])  > MAX_1H:
            tf_map['1h']  = tf_map['1h'][-MAX_1H:]


def _cleanup_cooldowns(cooldowns: dict) -> None:
    """만료된 cooldown 엔트리 제거 (메모리 누수 방지)"""
    now = time.time()
    expired = [k for k, v in cooldowns.items() if v < now]
    for k in expired:
        del cooldowns[k]


def _cleanup_inactive_slots(st: dict) -> None:
    """active=False이고 pending도 없는 슬롯 제거 (st 무한 증가 방지)"""
    now_ts = time.time()
    to_remove = [
        sym for sym, sym_st in st.items()
        if isinstance(sym_st, dict)
        and not is_active(sym_st)
        and not get_pending_entry(sym_st)
        # ★ 실패 쿨다운 중인 슬롯은 삭제하지 않음 (삭제 시 기억 소멸 → 무한 재시도)
        and float(sym_st.get('open_fail_cooldown_until', 0.0) or 0.0) < now_ts
        # [추가-1 FIX] exit_fail_cooldown도 보존 (삭제 시 청산 실패 기억 소멸)
        and float(sym_st.get('exit_fail_cooldown_until', 0.0) or 0.0) < now_ts
        and float(sym_st.get('last_open_ts', 0.0) or 0.0) < now_ts - 3600
    ]
    for sym in to_remove:
        del st[sym]
    if to_remove:
        print(f"[V9 Runner] 비활성 슬롯 {len(to_remove)}개 정리: {to_remove}")



def _rotate_logs() -> None:
    """
    log CSV rotation: 10MB 초과 시 .bak으로 이동 후 새 파일 시작
    """
    import shutil
    from v9.config import LOG_DIR
    MAX_BYTES = 10 * 1024 * 1024  # 10MB
    if not os.path.isdir(LOG_DIR):
        return
    for fname in os.listdir(LOG_DIR):
        if not fname.endswith('.csv'):
            continue
        fpath = os.path.join(LOG_DIR, fname)
        try:
            if os.path.getsize(fpath) > MAX_BYTES:
                bak = fpath.replace('.csv', f'_{int(time.time())}.bak')
                shutil.move(fpath, bak)
                print(f"[V9 Runner] log rotation: {fname} → {os.path.basename(bak)}")
        except Exception as _rot_e:
            print(f"[V9 Runner] log rotation 오류(무시): {_rot_e}")


# ═══════════════════════════════════════════════════════════════
# ★ v10.13: TP1 Limit 선주문 (maker 수수료 확보)
# ═══════════════════════════════════════════════════════════════
# 진입 체결 즉시 TP1 가격에 limit 주문 → 바이낸스가 매칭
# taker 0.045% → maker 0.018% (60% 절감)
# DCA 시 기존 취소 → 새 기준가로 재주문
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# ★ v10.13: Pending Limit 추적 (order_router fire-and-forget)
# ═══════════════════════════════════════════════════════════════
# order_router가 limit 주문 후 5초 체크 → 미체결 시 PENDING 등록
# runner가 매 틱마다 체결/타임아웃/취소 확인
# ═══════════════════════════════════════════════════════════════

_pending_limit_check_ts = 0.0
_PENDING_LIMIT_CHECK_SEC = 5.0


async def _manage_pending_limits(ex, st, snapshot):
    """
    order_router의 PENDING limit 주문 추적.
    병렬 fetch_order → 체결 시 포지션북 반영, 5분 타임아웃 시 취소.
    """
    global _pending_limit_check_ts
    now = time.time()
    if now - _pending_limit_check_ts < _PENDING_LIMIT_CHECK_SEC:
        return
    _pending_limit_check_ts = now

    from v9.execution.order_router import (
        get_pending_limits, remove_pending_limit,
        _clear_pending, PENDING_LIMIT_TIMEOUT_SEC,
    )
    from v9.logging.logger_csv import log_fill
    from v9.execution.position_book import ensure_slot, get_p, set_p

    pending = get_pending_limits()
    if not pending:
        return

    items = list(pending.items())  # [(oid, info), ...]

    # ── Phase 1: 병렬 fetch_order (5초 타임아웃) ──
    async def _safe_fetch(oid, sym):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(ex.fetch_order, oid, sym),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            return {"status": "_timeout"}
        except Exception as e:
            return {"status": "_error", "_err": str(e)}

    results = await asyncio.gather(
        *[_safe_fetch(oid, info["sym"]) for oid, info in items]
    )

    # ── Phase 2: 결과 처리 ──
    cancel_list = []  # timeout → 취소

    for (oid, info), fetch_result in zip(items, results):
        sym = info["sym"]
        status = fetch_result.get("status", "")

        if status.startswith("_"):
            # 타임아웃 체크 — 등록 후 5분 경과
            # ★ V10.29b: trim 선주문은 타임아웃 취소 제외 (체결까지 유지)
            if now - info["placed_at"] > PENDING_LIMIT_TIMEOUT_SEC and not info.get("is_trim"):
                cancel_list.append((oid, info))
            continue

        filled_qty = float(fetch_result.get("filled", 0) or 0)
        avg_price = float(fetch_result.get("average", 0) or info["price"] or 0)

        if status == "closed" or (status == "canceled" and filled_qty > 0):
            # ★ 체결 → 포지션북 반영
            _apply_pending_fill(st, info, filled_qty, avg_price, now, snapshot)
            log_fill(info["trace_id"], sym, info["side"], avg_price, filled_qty,
                     info["tag"], oid)
            _clear_pending(sym)
            remove_pending_limit(oid)
            # ★ v10.14b: pending_entry 반드시 해제 (_apply_pending_fill 실패해도)
            from v9.execution.position_book import set_pending_entry as _spe2
            ensure_slot(st, sym)
            # ★ v10.21: TP1 limit은 reduce → pending_entry 해제 불필요
            if info["intent_type"] != "TP1":
                _spe2(st[sym], info["side"], None)
            print(f"[PENDING_LIMIT] ★ {sym} {info['intent_type']} 체결! "
                  f"{filled_qty}@{avg_price:.4f}")
            # ★ V10.17: Pending limit 체결 텔레그램 알림
            if _TELEGRAM_OK:
                if info.get("is_trim"):
                    _pl_type = "TRIM_FILL"
                    # ★ V10.29d: TRIM ROI/PnL 계산
                    _trim_ep = float(info.get("entry_price", 0) or 0)
                    _trim_side = info["side"]  # trim side (close 방향)
                    if _trim_ep > 0 and avg_price > 0:
                        _raw = (avg_price - _trim_ep) / _trim_ep if _trim_side == "sell" else (_trim_ep - avg_price) / _trim_ep
                        _fee = (avg_price + _trim_ep) / _trim_ep * FEE_RATE
                        _trim_roi = (_raw - _fee) * LEVERAGE * 100
                        _trim_pnl = (_raw - _fee) * avg_price * filled_qty
                    else:
                        _trim_roi = _trim_pnl = 0.0
                elif info["intent_type"] == "TP1":
                    _pl_type = "TP1_LIMIT"
                    _trim_roi = _trim_pnl = 0.0
                elif info["intent_type"] == "DCA":
                    _pl_type = "PENDING_DCA"
                    _trim_roi = _trim_pnl = 0.0
                else:
                    _pl_type = "PENDING_OPEN"
                    _trim_roi = _trim_pnl = 0.0
                asyncio.ensure_future(_notify_async_fill(
                    sym, info["side"], avg_price, filled_qty, _pl_type,
                    pnl=_trim_pnl, roi=_trim_roi,
                    tier=info.get("tier", 0), role=info.get("role", ""),
                ))

        elif status == "canceled":
            remove_pending_limit(oid)
            _clear_pending(sym)
            from v9.execution.position_book import set_pending_entry as _spe
            ensure_slot(st, sym)
            _spe(st[sym], info["side"], None)
            # ★ V10.28b FIX: TP1 외부 취소 시 tp1_limit_oid + tp1_preorder_id 해제
            if info.get("intent_type") == "TP1":
                _cx_side = info.get("side", "")
                _cx_pos_side = "sell" if _cx_side == "buy" else "buy"
                _cx_p = get_p(st[sym], _cx_pos_side)
                if isinstance(_cx_p, dict):
                    _cx_p.pop("tp1_limit_oid", None)
                    if _cx_p.get("tp1_preorder_id") == str(oid):
                        _cx_p["tp1_preorder_id"] = None
                        _cx_p["tp1_preorder_price"] = None
                        _cx_p["tp1_preorder_ts"] = None
                        print(f"[TP1_PRE] {sym} 외부취소 → tp1_preorder_id 클리어")
                    # ★ V10.29b FIX: trim 선주문 외부 취소 → trim_preorders 정리
                    if info.get("is_trim"):
                        _cx_tier = info.get("tier", 0)
                        _trp = _cx_p.get("trim_preorders", {})
                        if _cx_tier in _trp:
                            _trp.pop(_cx_tier, None)
                            print(f"[TRIM_CLEANUP] {sym} T{_cx_tier} 외부취소 → trim_preorders 정리")
            print(f"[PENDING_LIMIT] {sym} 외부 취소")

        elif status == "open":
            # 타임아웃 체크
            # ★ V10.29b: trim 선주문은 타임아웃 취소 제외 (체결까지 유지)
            if now - info["placed_at"] > PENDING_LIMIT_TIMEOUT_SEC and not info.get("is_trim"):
                cancel_list.append((oid, info))

    # ── Phase 3: 타임아웃 취소 (병렬) ──
    if cancel_list:
        async def _safe_cancel(oid, sym):
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(ex.cancel_order, oid, sym),
                    timeout=5.0,
                )
                return True
            except Exception:
                return False

        cancel_results = await asyncio.gather(
            *[_safe_cancel(oid, info["sym"]) for oid, info in cancel_list]
        )
        for (oid, info), ok in zip(cancel_list, cancel_results):
            # 취소 후 부분 체결 확인
            part_filled = 0.0
            if ok:
                try:
                    chk = await asyncio.wait_for(
                        asyncio.to_thread(ex.fetch_order, oid, info["sym"]),
                        timeout=5.0,
                    )
                    part_filled = float(chk.get("filled", 0) or 0)
                    avg_p = float(chk.get("average", 0) or info["price"] or 0)
                    if part_filled > 0:
                        _apply_pending_fill(st, info, part_filled, avg_p, now, snapshot)
                        log_fill(info["trace_id"], info["sym"], info["side"],
                                 avg_p, part_filled, info["tag"] + "_PARTIAL", oid)
                        print(f"[PENDING_LIMIT] {info['sym']} 부분체결 {part_filled} 후 취소")
                        # ★ V10.17: 부분체결 텔레그램 알림
                        if _TELEGRAM_OK:
                            asyncio.ensure_future(_notify_async_fill(
                                info["sym"], info["side"], avg_p, part_filled,
                                "PENDING_DCA" if info["intent_type"] == "DCA" else "PENDING_OPEN",
                                tier=info.get("tier", 0), role=info.get("role", ""),
                            ))
                except Exception:
                    pass
            remove_pending_limit(oid)
            _clear_pending(info["sym"])
            if part_filled == 0:
                print(f"[PENDING_LIMIT] {info['sym']} 5분 미체결 → 취소")
            # ★ pending_entry 해제
            from v9.execution.position_book import set_pending_entry, ensure_slot
            ensure_slot(st, info["sym"])
            set_pending_entry(st[info["sym"]], info["side"], None)
            # ★ v10.24 Fix D: TP1 pending limit 타임아웃 시 exit_fail_cooldown 설정
            # 즉시 재생성 방지 (30초 쿨다운)
            if info.get("intent_type") == "TP1":
                st[info["sym"]]["exit_fail_cooldown_until"] = now + 30
                # ★ V10.28b FIX: tp1_limit_oid + tp1_preorder_id 해제 (유령 선주문 방지)
                _cancel_side = info.get("side", "")
                _cancel_pos_side = "sell" if _cancel_side == "buy" else "buy"
                _cancel_p = get_p(st[info["sym"]], _cancel_pos_side)
                if isinstance(_cancel_p, dict):
                    _cancel_p.pop("tp1_limit_oid", None)
                    # 선주문(tp1_preorder_id) 취소 시에도 클리어 → 유령 방지
                    if _cancel_p.get("tp1_preorder_id") == str(oid):
                        _cancel_p["tp1_preorder_id"] = None
                        _cancel_p["tp1_preorder_price"] = None
                        _cancel_p["tp1_preorder_ts"] = None
                        print(f"[TP1_PRE] {info['sym']} 타임아웃/취소 → tp1_preorder_id 클리어")
                    # ★ V10.29b FIX: trim 선주문 타임아웃 → trim_preorders 정리
                    if info.get("is_trim"):
                        _cancel_tier = info.get("tier", 0)
                        _cancel_trp = _cancel_p.get("trim_preorders", {})
                        if _cancel_tier in _cancel_trp:
                            _cancel_trp.pop(_cancel_tier, None)
                            print(f"[TRIM_CLEANUP] {info['sym']} T{_cancel_tier} 타임아웃 → trim_preorders 정리 (plan_tp1 DCA_TRIM 복귀)")


def _apply_pending_fill(st, info, filled_qty, avg_price, now, snapshot):
    """
    PENDING limit 체결 → 포지션북 반영.
    ★ v10.14: strategy_core.apply_order_results와 동일 수준 완전 반영
    OPEN: 새 포지션 생성 (dca_targets, locked_regime 포함)
    DCA: role 교차검증, tier 정확 적용, t5_split, locked_regime 갱신
    """
    from v9.execution.position_book import ensure_slot, get_p, set_p, iter_positions
    from v9.config import DCA_WEIGHTS, LEVERAGE

    sym = info["sym"]
    side = info["side"]
    itype = info["intent_type"]
    role = info.get("role", "CORE_MR")

    ensure_slot(st, sym)
    sym_st = st[sym]

    if itype == "OPEN":
        # 기존 포지션 있으면 role 교차 체크 (strategy_core [BUG-SH1] 미러링)
        existing = get_p(sym_st, side)
        if isinstance(existing, dict) and existing.get("role", "") and existing.get("role") != role:
            print(f"[PENDING_FILL] {sym} {side} role 충돌 {existing.get('role')} vs {role} → 무시")
            # ★ v10.14b: early return에서도 pending_entry 반드시 해제
            from v9.execution.position_book import set_pending_entry as _spe_er
            _spe_er(sym_st, side, None)
            return

        # ★ v10.14: info에서 dca_targets, locked_regime 등 복원
        _dca_targets = info.get("dca_targets", [])
        _locked_regime = info.get("locked_regime", "LOW")
        _entry_type = info.get("entry_type", "MR")
        _dca_level = info.get("dca_level", 1)

        set_p(sym_st, side, {
            "symbol":           sym,
            "side":             side,
            "ep":               avg_price,
            "original_ep":      avg_price,
            "amt":              filled_qty,
            "time":             now,
            "last_dca_time":    now,
            "atr":              info.get("atr", 0.0),
            "tag":              info["tag"],
            "step":             0,
            "dca_level":        _dca_level,
            "dca_targets":      _dca_targets,
            "max_roi_seen":     0.0,
            "worst_roi":        0.0,
            "pending_dca":      None,
            "trailing_on_time": None,
            "hedge_mode":       False,
            "tp1_done":         False,
            "tp2_done":         False,
            "entry_type":       _entry_type,
            "role":             role,
            "source_sym":       info.get("source_sym", ""),
            "source_side":      info.get("source_side", ""),
            "asym_forced":      False,
            "locked_regime":    _locked_regime,
            "hedge_entry_price": 0.0,
            "t5_entry_price":   0.0,
            "insurance_timecut": info.get("insurance_timecut", 0),
        })
        print(f"[PENDING_FILL] {sym} {side} OPEN 반영 ep={avg_price:.4f} "
              f"qty={filled_qty} role={role} dca_targets={len(_dca_targets)}개")

        from v9.execution.position_book import set_pending_entry
        set_pending_entry(sym_st, side, None)
        # ★ V10.29: 새 진입 → 같은 방향 min_slot_hold 해제
        for _ms_sym, _ms_ss in st.items():
            if not isinstance(_ms_ss, dict) or _ms_sym == sym:
                continue
            _ms_p = get_p(_ms_ss, side)
            if isinstance(_ms_p, dict) and _ms_p.get("min_slot_hold"):
                _ms_p["min_slot_hold"] = False
                print(f"[MIN_SLOT] {_ms_sym} {side} 교체 해제 ← 새 진입 {sym}")

    elif itype == "DCA":
        p = get_p(sym_st, side)
        if not (p and isinstance(p, dict) and avg_price > 0 and filled_qty > 0):
            print(f"[PENDING_FILL] {sym} DCA 대상 포지션 없음 — 무시")
            return

        # ★ v10.14: role 교차검증 (strategy_core DCA_GUARD 미러링)
        _expected_role = info.get("_expected_role", "")
        if _expected_role and p.get("role", "") != _expected_role:
            print(f"[PENDING_FILL_GUARD] {sym} {side} role 불일치! "
                  f"기대={_expected_role} 실제={p.get('role')} → DCA 차단")
            return

        # ★ v10.14: tier를 info에서 정확히 가져옴 (current+1 아닌 intent tier)
        tier = info.get("tier", 0)
        if tier <= 0:
            tier = int(p.get("dca_level", 1) or 1) + 1  # fallback

        # ★ v10.14: 이미 완료된 tier 가드
        _curr_dca = int(p.get("dca_level", 1) or 1)
        if tier <= _curr_dca:
            print(f"[PENDING_FILL] {sym} DCA T{tier} 이미 완료(현재 T{_curr_dca}) → 무시")
            return

        old_amt = float(p.get("amt", 0))
        old_ep = float(p.get("ep", 0))
        total_cost = (old_amt * old_ep) + (filled_qty * avg_price)
        p["amt"] = old_amt + filled_qty
        p["ep"] = total_cost / p["amt"] if p["amt"] > 0 else avg_price
        p["dca_level"] = tier
        p["last_dca_time"] = now
        p["time"] = now

        # ★ v10.14: 사용된 tier를 dca_targets에서 제거
        p["dca_targets"] = [
            t for t in p.get("dca_targets", []) if t.get("tier") != tier
        ]

        # tier별 entry price 기록
        if tier == 2: p["t2_entry_price"] = avg_price
        if tier == 3: p["t3_entry_price"] = avg_price
        if tier == 4: p["t4_entry_price"] = avg_price
        if tier == 5:
            p["t5_entry_price"] = avg_price
            p["max_dca_reached"] = True
            # ★ v10.14: T5 도달 → 반대 헷지 독립 모드 마킹 (t5_split)
            _opp_side = "sell" if side == "buy" else "buy"
            _opp_p = get_p(sym_st, _opp_side)
            if isinstance(_opp_p, dict) and _opp_p.get("role") == "CORE_HEDGE":
                _opp_p["t5_split"] = True
                print(f"[PENDING_FILL] T5_SPLIT {sym} 소스 T5 → 헷지 {_opp_side} 독립 모드")

        # ★ v10.14: locked_regime 갱신 (넓은 쪽 유지)
        _cur_regime = info.get("locked_regime", "")
        if _cur_regime:
            try:
                from v9.strategy.planners import _wider_regime
                p["locked_regime"] = _wider_regime(
                    p.get("locked_regime", "LOW"), _cur_regime
                )
            except Exception:
                pass

        p["pending_dca"] = None
        # ★ v10.15: DCA 체결 → insurance trigger 클리어
        p["insurance_sh_trigger"] = None
        p["tp1_preorder_id"] = None
        p["tp1_preorder_price"] = None
        # ★ V10.26: DCA 새 출발 — worst_roi/max_roi 0 리셋
        p["worst_roi"] = 0.0
        p["max_roi_seen"] = 0.0
        p["t4_defense"] = False  # ★ V10.29b: DCA 시 방어모드 리셋
        p["t4_worst_roi"] = 0.0
        p["last_dca_qty"] = filled_qty
        p.setdefault("dca_qty_by_tier", {})
        p["dca_qty_by_tier"][str(tier)] = filled_qty

        # ★ V10.28b: Trim 선주문 플래그
        if tier >= 2 and tier <= 4:
            from v9.config import calc_trim_price, calc_trim_qty, calc_tier_notional, notional_to_qty
            _pos_side = side  # DCA side = position side
            # ★ V10.29c FIX: 블렌디드 EP 기준 (기존 avg_price=DCA체결가 → 마이너스 trim 버그)
            _trim_price = calc_trim_price(float(p["ep"]), _pos_side, tier)
            # ★ V10.29d: 노셔널 기반 trim — 목표 tier 노셔널까지만 남기고 나머지 정리
            _bal = float(getattr(snapshot, 'real_balance_usdt', 0) or 0) if snapshot else 0
            _mark = float((snapshot.all_prices or {}).get(sym, 0) or 0) if snapshot else 0
            _trim_qty = calc_trim_qty(float(p["amt"]), tier, ep=float(p["ep"]), bal=_bal, mark_price=_mark)
            if _trim_qty <= 0:
                _trim_qty = filled_qty  # fallback: DCA 수량 그대로
            p.setdefault("trim_preorders", {})
            p["trim_to_place"] = {
                "tier": tier,
                "price": round(_trim_price, 8),
                "qty": _trim_qty,
                "side": "sell" if _pos_side == "buy" else "buy",
                "entry_price": float(p["ep"]),  # ★ V10.29c FIX: 블렌디드 EP
                "_ts": time.time(),  # ★ V10.29d: TTL용 타임스탬프
            }
            print(f"[TRIM_PREP] {sym} {_pos_side} T{tier}: "
                  f"선주문 준비 {_trim_qty:.4f}@${_trim_price:.4f} (ep={p['ep']:.4f}, notional-based)")

        print(f"[PENDING_FILL] {sym} {side} DCA T{tier} 반영 "
              f"ep={p['ep']:.4f} qty={p['amt']:.1f}")

    elif itype == "TP1":
        # ★ v10.21: TP1 지정가 체결 → 포지션 amt 감소 + step=1 전환
        # ★ V10.26b: side 수정 — TP1 주문 side(청산방향)의 반대가 포지션 side
        from v9.utils.utils_math import calc_roi_pct
        pos_side = "sell" if side == "buy" else "buy"  # ★ FIX: 포지션은 주문 반대방향
        p = get_p(sym_st, pos_side)
        if not (p and isinstance(p, dict)):
            print(f"[PENDING_FILL] {sym} TP1 대상 포지션 없음 (pos_side={pos_side}) — 무시")
            return

        old_ep = float(p.get("ep", 0))
        p["amt"] = max(0.0, float(p.get("amt", 0)) - filled_qty)
        p.pop("tp1_limit_oid", None)

        # ★ V10.29d: is_trim 우선 체크 (전량 매도 방지)
        if info.get("is_trim"):
            _target_tier = info.get("target_tier", max(1, int(p.get("dca_level", 2)) - 1))
            _old_tier = int(p.get("dca_level", 1))
            p["dca_level"] = _target_tier
            p["worst_roi"] = 0.0
            p["max_roi_seen"] = 0.0
            p["pending_dca"] = None
            p["step"] = 0
            p["tp1_done"] = False
            p["trailing_on_time"] = None
            p["tp1_preorder_id"] = None
            p["tp1_preorder_price"] = None
            _ep_keys = {2: "t2_entry_price", 3: "t3_entry_price", 4: "t4_entry_price"}
            for _t in range(_target_tier + 1, _old_tier + 1):
                if _t in _ep_keys:
                    p[_ep_keys[_t]] = 0.0
            try:
                from v9.strategy.planners import _build_dca_targets
                from v9.config import DCA_WEIGHTS as _TW, GRID_DIVISOR as _TG, LEVERAGE as _TL
                _trim_ep = float(p.get("ep", 0) or 0)
                _trim_bal = float(getattr(snapshot, 'real_balance_usdt', 0) or 0) if snapshot else 0
                if _trim_bal > 0:
                    _grid_est = _trim_bal / _TG * _TL
                else:
                    _trim_amt = float(p.get("amt", 0) or 0)
                    _cum_w = sum(_TW[:_target_tier])
                    _total_w = sum(_TW)
                    _grid_est = (_trim_ep * _trim_amt) / (_cum_w / _total_w) if _cum_w > 0 else _trim_ep * _trim_amt * 5
                p["dca_targets"] = [
                    t for t in _build_dca_targets(_trim_ep, pos_side, _grid_est, p.get("locked_regime", "LOW"))
                    if t.get("tier", 0) > _target_tier
                ]
            except Exception as _te:
                p["dca_targets"] = []
                print(f"[PENDING_FILL] {sym} dca_targets 재생성 실패: {_te}")
            print(f"[PENDING_FILL] {sym} {pos_side} DCA_TRIM T{_old_tier}→T{_target_tier} "
                  f"sold={filled_qty:.4f} remain={p['amt']:.4f} ep={p.get('ep',0):.4f}")
            if old_ep > 0:
                if pos_side == "buy":
                    _trim_pnl = filled_qty * (avg_price - old_ep) * LEVERAGE
                else:
                    _trim_pnl = filled_qty * (old_ep - avg_price) * LEVERAGE
                _trim_roi = calc_roi_pct(old_ep, avg_price, pos_side, LEVERAGE)
                _trim_icon = "✅" if _trim_pnl >= 0 else "🔴"
                print(f"[TRIM_FILL] {sym} {pos_side} T{_old_tier}→T{_target_tier} "
                      f"pnl=${_trim_pnl:+.2f} roi={_trim_roi:+.1f}%")
                try:
                    from telegram_engine import send_telegram_message
                    asyncio.ensure_future(send_telegram_message(
                        f"✂️ TRIM {sym.replace('/USDT','')} T{_old_tier}→T{_target_tier}\n"
                        f"{_trim_icon} ${_trim_pnl:+.2f} (roi={_trim_roi:+.1f}%)"))
                except Exception:
                    pass
                try:
                    from v9.logging.logger_csv import log_trade as _lt_trim
                    _hold = now - float(p.get("time", now) or now)
                    _lt_trim(
                        trace_id=f"trim_T{_old_tier}_{sym.replace('/','_')}",
                        symbol=sym, side=pos_side,
                        ep=old_ep, exit_price=avg_price, amt=filled_qty,
                        pnl_usdt=_trim_pnl, roi_pct=_trim_roi,
                        dca_level=_old_tier,
                        hold_sec=_hold if _hold > 0 else 0.0,
                        reason=f"TRIM_T{_old_tier}",
                        hedge_mode=False, was_hedge=False,
                        max_roi_seen=float(p.get("max_roi_seen", 0) or 0),
                        entry_type=str(p.get("entry_type", "MR") or "MR"),
                        role=str(p.get("role", "") or ""),
                        source_sym="",
                    )
                except Exception as _lt_e:
                    print(f"[TRIM_FILL] log_trade 실패(무시): {_lt_e}")
            _trp = p.get("trim_preorders", {})
            _trp.pop(_old_tier, None)
            if _target_tier <= 1:
                p["trim_preorders"] = {}

        elif p["amt"] <= 0:
            # 전량 체결 → 포지션 클리어
            from v9.execution.position_book import clear_position
            from v9.logging.logger_csv import log_trade
            _hold = now - float(p.get("time", now) or now)
            _roi = calc_roi_pct(old_ep, avg_price, pos_side, LEVERAGE) if old_ep > 0 else 0
            if pos_side == "buy":
                _pnl = filled_qty * (avg_price - old_ep)
            else:
                _pnl = filled_qty * (old_ep - avg_price)
            log_trade(
                trace_id=info["trace_id"], symbol=sym, side=pos_side,
                ep=old_ep, exit_price=avg_price, amt=filled_qty,
                pnl_usdt=_pnl, roi_pct=_roi,
                dca_level=int(p.get("dca_level", 1) or 1),
                hold_sec=_hold, reason="TP1_LIMIT_FULL",
                hedge_mode=bool(p.get("hedge_mode", False)),
                was_hedge=bool(p.get("was_hedge", False)),
                max_roi_seen=float(p.get("max_roi_seen", 0) or 0),
                entry_type=str(p.get("entry_type", "MR") or "MR"),
                role=str(p.get("role", "") or ""),
                source_sym=str(p.get("source_sym", "") or ""),
            )
            clear_position(st, sym, pos_side)
            print(f"[PENDING_FILL] {sym} {pos_side} TP1 전량체결 → 클리어")

        else:
            # 부분 체결 → step=1 + trailing 전환
            p["step"] = 1
            p["tp1_done"] = True
            p["tp1_price"] = avg_price
            p["trailing_on_time"] = now
            dca = int(p.get("dca_level", 1) or 1)
            if pos_side == "buy":
                _pnl = filled_qty * (avg_price - old_ep)
            else:
                _pnl = filled_qty * (old_ep - avg_price)
            _roi = calc_roi_pct(old_ep, avg_price, pos_side, LEVERAGE) if old_ep > 0 else 0
            print(f"[PENDING_FILL] {sym} {pos_side} TP1 T{dca} 체결 "
                  f"{filled_qty}@{avg_price:.4f} pnl=${_pnl:.2f} roi={_roi:.1f}% "
                  f"→ trailing(잔량={p['amt']:.1f})")



# ═════════════════════════════════════════════════════════════════
# ★ V10.28b: 일별 PnL 자동 리포트
# ═════════════════════════════════════════════════════════════════
_last_report_date = ""

async def _daily_pnl_report(st):
    """매일 00:05 UTC 자동 실행 — 전일 트레이드 요약 텔레그램 발송."""
    global _last_report_date
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    if not (now.hour == 0 and 5 <= now.minute <= 10):
        return
    today = now.strftime("%Y-%m-%d")
    if _last_report_date == today:
        return
    _last_report_date = today

    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        from v9.execution.position_book import iter_positions as _ip
        active = sum(
            1 for _ss in st.values() if isinstance(_ss, dict)
            for _, _p in _ip(_ss)
            if isinstance(_p, dict) and float(_p.get("amt", 0) or 0) > 0
        )

        if _TELEGRAM_OK:
            from telegram_engine import send_daily_report
            await send_daily_report(yesterday, active_positions=active)
            print(f"[DAILY_REPORT] {yesterday} 발송 완료")
    except Exception as e:
        print(f"[DAILY_REPORT] 오류: {e}")


# ═════════════════════════════════════════════════════════════════
# ★ V10.28b: Trim 선주문 관리
# ═════════════════════════════════════════════════════════════════
async def _place_trim_preorders(ex, st, snapshot):
    """DCA 체결 후 trim_to_place 플래그 → 바이낸스 limit 주문 + pending_limits 등록."""
    from v9.execution.position_book import ensure_slot, get_p, iter_positions
    from v9.execution.order_router import _PENDING_LIMITS
    from v9.config import SYM_MIN_QTY, SYM_MIN_QTY_DEFAULT, LEVERAGE
    import asyncio

    for sym, sym_st in st.items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if not isinstance(p, dict):
                continue

            # ★ V10.29b: stale trim_preorders 정리 — 거래소에 없는 주문 참조 제거
            _trp = p.get("trim_preorders")
            if _trp and isinstance(_trp, dict):
                _stale_tiers = [
                    t for t, info in _trp.items()
                    if info.get("oid") and str(info["oid"]) not in _PENDING_LIMITS
                ]
                for _st in _stale_tiers:
                    _trp.pop(_st, None)
                    print(f"[TRIM_STALE] {sym} {pos_side} T{_st} trim_preorders 정리 "
                          f"(oid not in pending_limits → plan_tp1 DCA_TRIM 복귀)")

            ttp = p.get("trim_to_place")
            if not ttp:
                # ★ V10.29d: trim 재생성 — 노셔널 기반
                _regen_dca = int(p.get("dca_level", 1) or 1)
                _regen_trp = p.get("trim_preorders", {})
                if _regen_dca >= 2 and not _regen_trp and p.get("ep") and p.get("amt"):
                    from v9.config import calc_trim_price, calc_trim_qty
                    _regen_ep = float(p.get("ep", 0))
                    _regen_bal = float(getattr(snapshot, 'real_balance_usdt', 0) or 0) if snapshot else 0
                    _regen_mark = float((snapshot.all_prices or {}).get(sym, 0) or 0) if snapshot else 0
                    _regen_qty = calc_trim_qty(
                        float(p["amt"]), _regen_dca,
                        ep=_regen_ep, bal=_regen_bal, mark_price=_regen_mark
                    )
                    _regen_price = calc_trim_price(_regen_ep, pos_side, _regen_dca)
                    if _regen_qty > 0 and _regen_price > 0:
                        p["trim_to_place"] = {
                            "tier": _regen_dca,
                            "price": round(_regen_price, 8),
                            "qty": _regen_qty,
                            "side": "sell" if pos_side == "buy" else "buy",
                            "entry_price": _regen_ep,
                            "_ts": time.time(),
                        }
                        ttp = p["trim_to_place"]
                        print(f"[TRIM_REGEN] {sym} {pos_side} T{_regen_dca}: "
                              f"trim 재생성 {_regen_qty:.4f}@${_regen_price:.4f} (notional-based)")
                    else:
                        continue
                else:
                    continue

            tier = ttp["tier"]
            trim_price = ttp["price"]
            trim_qty = ttp["qty"]
            order_side = ttp["side"]  # sell for long, buy for short
            entry_price = ttp["entry_price"]

            # 최소 수량 체크
            min_qty = SYM_MIN_QTY.get(sym, SYM_MIN_QTY_DEFAULT)
            if trim_qty < min_qty:
                print(f"[TRIM_SKIP] {sym} T{tier} qty {trim_qty:.4f} < min {min_qty}")
                p.pop("trim_to_place", None)
                continue

            # positionSide (hedge mode)
            ps = "LONG" if pos_side == "buy" else "SHORT"

            try:
                # ★ V10.28b FIX: 정밀도 라운딩 (Binance LOT_SIZE/PRICE_FILTER 통과)
                safe_trim_qty = float(ex.amount_to_precision(sym, trim_qty))
                safe_trim_price = float(ex.price_to_precision(sym, trim_price))
                if safe_trim_qty <= 0 or safe_trim_price <= 0:
                    print(f"[TRIM_SKIP] {sym} T{tier} precision → 0")
                    p.pop("trim_to_place", None)
                    continue
                order = await asyncio.to_thread(
                    ex.create_order,
                    sym, "limit", order_side, safe_trim_qty, safe_trim_price,
                    params={"positionSide": ps}
                )
                oid = str(order.get("id", ""))
                if oid:
                    # pending_limits 등록 (is_trim + target_tier 포함)
                    _PENDING_LIMITS[oid] = {
                        "sym": sym,
                        "side": order_side,
                        "qty": trim_qty,
                        "price": trim_price,
                        "trace_id": f"trim_T{tier}_{sym}",
                        "tag": f"V9_TRIM_T{tier}_{sym}",
                        "placed_at": __import__("time").time(),
                        "intent_type": "TP1",
                        "positionSide": ps,
                        "role": p.get("role", "CORE_MR"),
                        "tier": tier,
                        "is_trim": True,
                        "target_tier": tier - 1,
                        "_expected_role": p.get("role", ""),
                    }
                    # position에 trim 주문 기록
                    p.setdefault("trim_preorders", {})[tier] = {
                        "oid": oid, "price": safe_trim_price, "qty": safe_trim_qty,
                    }
                    print(f"[TRIM_PLACED] {sym} {pos_side} T{tier}: "
                          f"{order_side} {safe_trim_qty}@${safe_trim_price:.4f} oid={oid}")
                    try:
                        from telegram_engine import send_telegram_message
                        asyncio.ensure_future(send_telegram_message(
                            f"📋 TRIM T{tier} {sym.replace('/USDT','')}\n"
                            f"{order_side} {safe_trim_qty}@${safe_trim_price:.4f}"))
                    except Exception:
                        pass
                else:
                    print(f"[TRIM_FAIL] {sym} T{tier}: 주문 ID 없음")
                p.pop("trim_to_place", None)
            except Exception as e:
                # ★ V10.29: 실패 시 trim_to_place 유지 → 다음 틱 재시도
                _retry = int(ttp.get("_retry", 0))
                if _retry >= 3:
                    p.pop("trim_to_place", None)
                    print(f"[TRIM_ERR] {sym} T{tier}: 3회 실패 포기 — {e}")
                    try:
                        from telegram_engine import send_telegram_message
                        asyncio.ensure_future(send_telegram_message(
                            f"⚠️ TRIM 실패 {sym.replace('/USDT','')} T{tier}\n{str(e)[:80]}"))
                    except Exception:
                        pass
                else:
                    ttp["_retry"] = _retry + 1
                    print(f"[TRIM_ERR] {sym} T{tier}: 재시도 {_retry+1}/3 — {e}")


async def _cancel_trim_preorders(ex, st, sym, pos_side):
    """포지션 청산 시 해당 심볼의 trim 선주문 전량 취소."""
    from v9.execution.position_book import get_p
    from v9.execution.order_router import _PENDING_LIMITS, remove_pending_limit
    import asyncio

    sym_st = st.get(sym, {})
    p = get_p(sym_st, pos_side)
    if not isinstance(p, dict):
        return

    trim_orders = p.get("trim_preorders", {})
    if not trim_orders:
        return

    for tier, info in list(trim_orders.items()):
        oid = info.get("oid", "")
        if not oid:
            continue
        try:
            await asyncio.to_thread(ex.cancel_order, oid, sym)
            remove_pending_limit(oid)
            print(f"[TRIM_CANCEL] {sym} {pos_side} T{tier} oid={oid} 취소")
        except Exception as e:
            # 이미 체결/취소된 경우 무시
            remove_pending_limit(oid)
            print(f"[TRIM_CANCEL] {sym} T{tier} 취소 시도: {e}")

    p["trim_preorders"] = {}


async def _main_loop(ex_init, dry_run: bool):
    """V9 메인 루프"""
    print(f"[V9 Runner] 시작 (dry_run={dry_run})")
    ex = ex_init  # 재연결 시 교체 가능

    # ★ Beta Cycle 초기화
    if _BC_ENABLED:
        bc_init(ex)
        print("[V9 Runner] Beta Cycle 엔진 활성화")

    # ★ Crash Bounce 초기화
    if _CB_ENABLED:
        cb_init(ex)
        print("[V9 Runner] Crash Bounce 엔진 활성화")

    # ── 상태 로드 ────────────────────────────────────────────────
    book = load_position_book()
    st           = book['st']
    cooldowns    = book['cooldowns']
    system_state = book['system_state']

    snapshot: MarketSnapshot = None  # type: ignore
    last_universe_ts   = 0.0
    last_save_ts       = 0.0
    last_hb_ts         = 0.0
    last_cleanup_ts    = 0.0   # cooldown 정리 + log rotation 주기
    start_ts           = time.time()
    _prev_balance      = 0.0   # ★ 잔고 급변 방어용
    _leverage_set      = False  # ★ 레버리지 초기화 플래그

    # ★ v10.12: 부팅 시각 기록 (INSURANCE_SH 오발동 방지)
    system_state["_boot_ts"] = start_ts

    # ★ V10.27e: 글로벌 전략/헷지 state 복원
    from v9.strategy.planners import restore_strategy_state
    from v9.engines.hedge_core import restore_hedge_state
    restore_strategy_state(system_state)
    restore_hedge_state(system_state)
    # ★ V10.29c: BC/CB state 복원
    try:
        from v9.engines.beta_cycle import bc_restore_state
        from v9.engines.crash_bounce import cb_restore_state
        bc_restore_state(system_state)
        cb_restore_state(system_state)
    except Exception as _e:
        print(f"[BOOT] BC/CB state restore 실패(무시): {_e}")

    # ★ v10.15: minroi 상태 로드
    _minroi = load_minroi()
    _last_minroi_save_ts = start_ts

    # ★ v10.15: HIGH sticky 타이머
    system_state.setdefault("high_enter_ts", 0.0)

    # ★ v10.12: 부팅 시 미체결 주문 전량 취소
    # 이전 세션에서 limit 주문이 남아있으면 체결 시 포지션북 미반영 → dca_level 꼬임
    try:
        from v9.config import MAJOR_UNIVERSE as _CANCEL_SYMS
        _cancel_count = 0
        for _csym in _CANCEL_SYMS:
            try:
                _open_orders = await asyncio.to_thread(ex.fetch_open_orders, _csym)
                for _oo in _open_orders:
                    _oid = _oo.get('id')
                    if _oid:
                        await asyncio.to_thread(ex.cancel_order, _oid, _csym)
                        _cancel_count += 1
                        print(f"[STARTUP] {_csym} 미체결 주문 취소: {_oid} "
                              f"({_oo.get('side','')} {_oo.get('amount','')} @ {_oo.get('price','')})")
            except Exception as _co_e:
                if '2011' not in str(_co_e):  # Unknown order = 이미 체결/취소
                    print(f"[STARTUP] {_csym} 주문 조회/취소 오류(무시): {str(_co_e)[:60]}")
            await asyncio.sleep(0.05)  # rate limit 방지
        if _cancel_count > 0:
            print(f"[STARTUP] ★ 미체결 주문 {_cancel_count}건 취소 완료")
            _save_all(st, cooldowns, system_state)
        else:
            print(f"[STARTUP] 미체결 주문 없음")
    except Exception as _startup_e:
        print(f"[STARTUP] 미체결 주문 정리 실패(무시): {_startup_e}")

    # ★ FIX-1: 부팅 시 pending_entry + tp1_preorder_id 전부 클리어
    # 이전 세션의 limit 주문은 위에서 전부 취소했으므로, state에 남은 건 전부 유령
    _startup_clear_count = 0
    for _sc_sym, _sc_ss in st.items():
        if not isinstance(_sc_ss, dict):
            continue
        for _sc_key in ('pending_entry_long', 'pending_entry_short'):
            if _sc_ss.get(_sc_key):
                _sc_ss[_sc_key] = None
                _startup_clear_count += 1
        for _sc_side_key in ('p_long', 'p_short'):
            _sc_p = _sc_ss.get(_sc_side_key)
            if isinstance(_sc_p, dict) and _sc_p.get('tp1_preorder_id'):
                _sc_p['tp1_preorder_id'] = None
                _sc_p['tp1_preorder_price'] = None
                _sc_p['tp1_preorder_ts'] = None
                _startup_clear_count += 1
            # pending_dca도 클리어 (이전 limit DCA 미체결 잔여)
            if isinstance(_sc_p, dict) and _sc_p.get('pending_dca'):
                _sc_p['pending_dca'] = None
                _startup_clear_count += 1
            # ★ V10.22: tp_locked 레거시 필드 정리 (재시작 시 클리어)
            if isinstance(_sc_p, dict):
                for _legacy_key in ('tp_locked', 'tp_lock_reason', 'tp_lock_ts', 'tp_lock_force_dca'):
                    if _sc_p.get(_legacy_key):
                        _sc_p.pop(_legacy_key, None)
                        _startup_clear_count += 1
    if _startup_clear_count > 0:
        print(f"[STARTUP] ★ state 유령 {_startup_clear_count}건 클리어 "
              f"(pending_entry + tp1_preorder + pending_dca)")
        _save_all(st, cooldowns, system_state)

    # ★ v10.18: 다운타임 중 청산 감지 & 알림
    try:
        await _check_downtime_trades(ex, st, system_state)
    except Exception as _dt_e:
        print(f"[STARTUP] 다운타임 감지 실패(무시): {_dt_e}")

    # ★ v10.17: _skew_stage2_enter_ts 복원 (재시작 후 15분 타이머 유지)
    try:
        import v9.engines.hedge_core as _hc_mod
        _saved_s2ts = float(system_state.get('_skew_stage2_enter_ts', 0.0) or 0.0)
        if _saved_s2ts > 0 and time.time() - _saved_s2ts < 3600:  # 1시간 이내만 복원
            _hc_mod._skew_stage2_enter_ts = _saved_s2ts
            _elapsed = (time.time() - _saved_s2ts) / 60
            print(f"[STARTUP] _skew_stage2_enter_ts 복원: {_elapsed:.0f}분 경과")
        else:
            _hc_mod._skew_stage2_enter_ts = 0.0
    except Exception as _s2e:
        print(f"[STARTUP] stage2 타이머 복원 실패(무시): {_s2e}")

    while True:
        now = time.time()
        loop_start = now

        try:
            # ── ★ v10.17: config_override.json 핫리로드 ─────────────
            # 파일 있으면 v9.config 모듈 속성을 런타임 오버라이드
            # 파일 없으면 기존 동작 완전히 동일 (zero-risk)
            # 허용 키만 오버라이드 (안전 화이트리스트)
            _OVERRIDE_WHITELIST = {
                "SKEW_STAGE2_TRIGGER", "SKEW_STAGE2_TIMEOUT_SEC",
                "SKEW_HEDGE_STRESS_ROI", "SKEW_HEDGE_TRIGGER",
                "REBOUND_ALPHA",
            }
            _override_path = os.path.join(_PROJECT_DIR, "config_override.json")
            if os.path.exists(_override_path):
                try:
                    import v9.config as _v9cfg
                    with open(_override_path, encoding='utf-8') as _ov_f:
                        _overrides = json.load(_ov_f)
                    for _ok, _ov in _overrides.items():
                        if _ok in _OVERRIDE_WHITELIST and hasattr(_v9cfg, _ok):
                            setattr(_v9cfg, _ok, _ov)
                except Exception as _ov_e:
                    print(f"[OVERRIDE] config_override.json 로드 실패(무시): {_ov_e}")

            # ── 텔레그램 봇 명령 싱크 (system_state.json → in-memory) ───
            try:
                _ss_path = os.path.join(_PROJECT_DIR, "system_state.json")
                if os.path.exists(_ss_path):
                    with open(_ss_path, encoding='utf-8') as _f:
                        _ss_ext = json.load(_f)
                    # 봇이 쓴 명령 키만 in-memory system_state로 병합
                    for _cmd_key in (
                        "close_all_requested", "close_all_mode",
                        "use_long", "use_short",
                        "shutdown_active", "shutdown_reason", "is_locked",
                        "baseline_balance", "baseline_date", "initial_balance",
                    ):
                        if _cmd_key in _ss_ext:
                            system_state[_cmd_key] = _ss_ext[_cmd_key]
            except Exception as _ss_e:
                print(f"[V9 Runner] state sync 오류(무시): {_ss_e}")

            # ── 하트비트 ─────────────────────────────────────────
            if now - last_hb_ts >= 10:
                try:
                    with open(HEARTBEAT_FILE, 'w') as f:
                        f.write(str(now))
                except Exception as _hb_e:
                    print(f"[V9 Runner] heartbeat 오류(무시): {_hb_e}")
                last_hb_ts = now

            # ── 주기 정리 (1시간마다) ────────────────────────────
            if now - last_cleanup_ts >= 3600:
                _cleanup_cooldowns(cooldowns)
                _cleanup_inactive_slots(st)
                _rotate_logs()
                last_cleanup_ts = now

            # ── 활성 심볼 목록 ───────────────────────────────────
            active_syms = []
            for sym, sym_st in st.items():
                if is_active(sym_st) or get_pending_entry(sym_st):
                    active_syms.append(sym)
            if snapshot:
                active_syms += snapshot.global_targets_long
                active_syms += snapshot.global_targets_short
            active_syms = list(set(active_syms))

            # ── 스냅샷 수집 ──────────────────────────────────────
            snapshot = await fetch_market_snapshot(
                ex, active_syms, prev_snapshot=snapshot
            )

            # ── ohlcv 메모리 누수 방지 ───────────────────────────
            _trim_ohlcv_pool(snapshot)

            if not snapshot.valid:
                print(f"[V9 Runner] 스냅샷 유효하지 않음 — 스킵")
                try:
                    _write_system_state_compat(snapshot, system_state, st)
                except Exception:
                    pass
                await asyncio.sleep(2)
                continue

        # ── 구 reconcile 제거 (v10.11b) ─────────────────────
            # ★ 기존 10초 reconcile이 롱/숏 구분 없이 한쪽 ep를 양쪽에 덮어쓰는 버그
            # → 새 _sync_positions_with_exchange()가 (sym, side) 키로 정확히 매칭
            # → 구 reconcile 완전 제거


            # ── 초기 잔고 설정 ───────────────────────────────────
            if system_state.get('initial_balance', 0.0) <= 0:
                system_state['initial_balance'] = snapshot.real_balance_usdt

            # ── baseline 갱신 (셧다운 중 금지) ───────────────────
            if not system_state.get('shutdown_active', False):
                today = today_str()
                if system_state.get('baseline_date', '') != today:
                    system_state['baseline_balance'] = snapshot.real_balance_usdt
                    system_state['baseline_date'] = today
                    from dataclasses import replace
                    snapshot = replace(snapshot, baseline_balance=snapshot.real_balance_usdt)

            # baseline을 snapshot에 반영
            from dataclasses import replace as dc_replace
            snapshot = dc_replace(snapshot, baseline_balance=system_state.get('baseline_balance', snapshot.real_balance_usdt))

            # ── DD -5% 셧다운 트리거 ─────────────────────────────
            baseline = system_state.get('baseline_balance', 0.0)
            current  = snapshot.real_balance_usdt
            if (baseline > 0 and current > 0
                    and not system_state.get('shutdown_active', False)):
                dd_pct = (current - baseline) / baseline
                if dd_pct <= DD_SHUTDOWN_THRESHOLD:
                    system_state['shutdown_active'] = True
                    system_state['shutdown_until'] = now + DD_SHUTDOWN_HOURS * 3600
                    system_state['shutdown_reason'] = f"DD {dd_pct*100:.2f}%"
                    print(f"[V9 Runner] DD 셧다운 발동! dd={dd_pct*100:.2f}%")
                    _save_all(st, cooldowns, system_state)

            # ── 셧다운 만료 체크 ─────────────────────────────────
            if system_state.get('shutdown_active', False):
                if now >= system_state.get('shutdown_until', 0.0):
                    system_state['shutdown_active'] = False
                    system_state['shutdown_reason'] = ''
                    print("[V9 Runner] 셧다운 만료 → 정상 복귀")
                    _save_all(st, cooldowns, system_state)

            # ── Kill Switch 상태 업데이트 ────────────────────────
            mr = snapshot.margin_ratio
            if mr >= 0.8:
                system_state['allow_new_entries'] = False
                system_state['allow_dca'] = False
            elif mr >= 0.7:
                system_state['allow_new_entries'] = False
                system_state['allow_dca'] = True
            else:
                system_state['allow_new_entries'] = True
                system_state['allow_dca'] = True

            # ── Universe 업데이트 (5분 주기) ─────────────────────
            if now - last_universe_ts >= 300:
                snapshot = await update_universe(ex, snapshot)
                last_universe_ts = now

            # ── 활성화 임계값 체크 ───────────────────────────────
            if snapshot.real_balance_usdt < ACTIVATION_THRESHOLD:
                print(f"[V9 Runner] 잔고 부족 ({snapshot.real_balance_usdt:.2f} < {ACTIVATION_THRESHOLD}) — 대기")
                await asyncio.sleep(5)
                continue

            # ★ 레버리지 초기세팅 (1회) — 수동 변경으로 config 불일치 방지
            if not _leverage_set:
                from v9.config import MAJOR_UNIVERSE as _MU
                _lev_ok = 0
                for _lsym in _MU:
                    try:
                        await asyncio.to_thread(ex.set_leverage, LEVERAGE, _lsym)
                        _lev_ok += 1
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
                _leverage_set = True
                print(f"[V9 Runner] 레버리지 {LEVERAGE}x 세팅 완료 ({_lev_ok}/{len(_MU)} 심볼)")

            # ── 시작 후 3분 워밍업 ───────────────────────────────
            if now - start_ts < 180:
                await asyncio.sleep(1)
                continue

            # ★ 잔고 급변 방어: 직전 틱 대비 ±20% 이상 변동 시 신규 주문 스킵
            _cur_bal = float(snapshot.real_balance_usdt or 0.0)
            if _prev_balance > 0 and _cur_bal > 0:
                _bal_change = abs(_cur_bal - _prev_balance) / _prev_balance
                if _bal_change > 0.20:
                    print(f"[V9 Runner] ⚠ 잔고 급변 감지: {_prev_balance:.2f} → {_cur_bal:.2f} "
                          f"({_bal_change*100:.1f}%) — 이번 틱 신규 주문 스킵")
                    _prev_balance = _cur_bal
                    await asyncio.sleep(1)
                    continue
            _prev_balance = _cur_bal

            # ★ max_roi_seen 매틱 갱신 — plan_trail_on은 step≥1에서만 갱신하므로
            # step=0 포지션의 max_roi가 ZOMBIE 판단에 영향
            _prices_mr = snapshot.all_prices or {}
            for _mr_sym, _mr_st in st.items():
                for _mr_side, _mr_p in iter_positions(_mr_st):
                    if _mr_p is None:
                        continue
                    _mr_ep = float(_mr_p.get("ep", 0.0) or 0.0)
                    _mr_cp = float(_prices_mr.get(_mr_sym, 0.0) or 0.0)
                    if _mr_ep > 0 and _mr_cp > 0:
                        from v9.utils.utils_math import calc_roi_pct as _calc_roi
                        _mr_roi = _calc_roi(_mr_ep, _mr_cp, _mr_side, LEVERAGE)
                        _mr_max = float(_mr_p.get("max_roi_seen", 0.0) or 0.0)
                        if _mr_roi > _mr_max:
                            _mr_p["max_roi_seen"] = _mr_roi
                        # ★ v10.14c: worst_roi 매틱 갱신 (min_roi 반등 TP1용)
                        _mr_worst = float(_mr_p.get("worst_roi", 0.0) or 0.0)
                        if _mr_roi < _mr_worst:
                            _mr_p["worst_roi"] = _mr_roi
                        # ★ v10.15: minroi JSON도 갱신
                        _mr_dca = int(_mr_p.get("dca_level", 1) or 1)
                        update_minroi(_minroi, _mr_sym, _mr_side, _mr_roi, _mr_dca)

            # ── 텔레그램 전체 청산 요청 ───────────────────────────
            if system_state.get("close_all_requested"):
                _close_mode = system_state.pop("close_all_mode", "market")
                system_state["close_all_requested"] = False
                _close_intents = []
                for _csym, _cst in st.items():
                    for _, _cp in iter_positions(_cst):
                        if _cp is None:
                            continue
                        _ccurr = (snapshot.all_prices or {}).get(_csym, 0)
                        if _ccurr <= 0:
                            continue
                        _clong = (_cp.get("side", "") == "buy")
                        from v9.types import Intent as _I
                        _close_intents.append(_I(
                            trace_id=str(uuid.uuid4())[:8],
                            intent_type=IntentType.FORCE_CLOSE,
                            symbol=_csym,
                            side="sell" if _clong else "buy",
                            qty=float(_cp.get("amt", 0)),
                            price=_ccurr if _close_mode == "limit" else None,
                            reason=f"TELEGRAM_CLOSE_ALL_{_close_mode.upper()}",
                            metadata={"telegram_close": True},
                        ))
                if _close_intents:
                    _cr = await execute_intents(ex, _close_intents, dry_run=dry_run, st=st)
                    _cm = {i.trace_id: i for i in _close_intents}
                    apply_order_results(_cr, _cm, st, cooldowns, snapshot)
                    _save_all(st, cooldowns, system_state)
                    print(f"[V9] 텔레그램 전체 청산: {len(_close_intents)}건 ({_close_mode})")
                    continue

            # ★ v10.6: 현재 레짐 기록 (텔레그램 봇 표시용)
            try:
                from v9.strategy.planners import _btc_vol_regime
                system_state["_current_regime"] = _btc_vol_regime(snapshot)
            except Exception:
                pass

            # ★ v10.24 Fix C: RECOVERED 좀비 자동 청산
            # tag=V9_RECOVERED + step=0 + 30분 경과 → FORCE_CLOSE
            # ★ V10.26b: 위험한 강제 클리어 제거 — 거래소 포지션 보호
            #   - amt=0만 클리어 (확실히 없는 경우)
            #   - 3회 실패 → _zombie_stuck=True → 재시도 중단 (포지션은 유지)
            #   - 60초 간격으로만 재시도 (스팸 방지)
            _recovered_close_intents = []
            from v9.types import Intent as _Intent_rc
            for _rc_sym, _rc_ss in st.items():
                if not isinstance(_rc_ss, dict):
                    continue
                for _rc_side, _rc_p in iter_positions(_rc_ss):
                    if _rc_p is None:
                        continue
                    if str(_rc_p.get("tag", "")) != "V9_RECOVERED":
                        continue
                    if int(_rc_p.get("step", 0) or 0) != 0:
                        continue
                    if now - float(_rc_p.get("time", now) or now) <= 1800:
                        continue

                    # amt=0 → 안전하게 클리어
                    _rc_amt = float(_rc_p.get("amt", 0) or 0)
                    if _rc_amt <= 0:
                        from v9.execution.position_book import clear_position as _cp_rc
                        _cp_rc(st, _rc_sym, _rc_side)
                        print(f"[V9] RECOVERED amt=0 클리어: {_rc_sym} {_rc_side}")
                        continue

                    # 이미 stuck → 건너뜀 (다음 sync가 정리)
                    if _rc_p.get("_zombie_stuck"):
                        continue

                    # 60초 간격 재시도 제한
                    _last_try = float(_rc_p.get("_zombie_last_try", 0) or 0)
                    if now - _last_try < 60:
                        continue
                    _rc_p["_zombie_last_try"] = now

                    # 3회 실패 → stuck 마킹 (절대 포지션 삭제하지 않음)
                    _retry = int(_rc_p.get("_zombie_retry", 0) or 0)
                    if _retry >= 3:
                        _rc_p["_zombie_stuck"] = True
                        print(f"[V9] RECOVERED 좀비 3회 실패 → stuck 마킹 (수동 확인 필요): {_rc_sym} {_rc_side}")
                        continue
                    _rc_p["_zombie_retry"] = _retry + 1

                    _rc_cp = float((snapshot.all_prices or {}).get(_rc_sym, 0) or 0)
                    if _rc_cp > 0:
                        _rc_close_side = "sell" if _rc_side == "buy" else "buy"
                        _recovered_close_intents.append(_Intent_rc(
                            trace_id=str(uuid.uuid4())[:8],
                            intent_type=IntentType.FORCE_CLOSE,
                            symbol=_rc_sym,
                            side=_rc_close_side,
                            qty=_rc_amt,
                            price=None,
                            reason="RECOVERED_ZOMBIE_30MIN",
                            metadata={"positionSide": "LONG" if _rc_side == "buy" else "SHORT"},
                        ))
            if _recovered_close_intents:
                _rc_results = await execute_intents(
                    ex, _recovered_close_intents, dry_run=dry_run, st=st
                )
                _rc_map = {i.trace_id: i for i in _recovered_close_intents}
                apply_order_results(_rc_results, _rc_map, st, cooldowns, snapshot)
                _save_all(st, cooldowns, system_state)

            # ── Intent 생성 ──────────────────────────────────────
            intents = generate_all_intents(snapshot, st, cooldowns, system_state)
            # ★ v10.14d: plan_dca는 generate_all_intents 안에서 실행 (보험 타이밍 수정)
            intents += generate_corrguard_intents(snapshot, st, system_state)

            # ★ Beta Cycle 통합
            if _BC_ENABLED:
                # 일봉 마감 감지 (UTC 00:00 직후, 1일 1회)
                _bc_hour = int(time.strftime("%H", time.gmtime()))
                _bc_today = time.strftime("%Y-%m-%d", time.gmtime())
                if not hasattr(_main_loop, '_bc_last_daily'):
                    _main_loop._bc_last_daily = ""
                if _bc_hour == 0 and _main_loop._bc_last_daily != _bc_today:
                    _main_loop._bc_last_daily = _bc_today
                    try:
                        # ★ V10.29b-BC FIX: 동기 fetch → 별도 스레드 (MR 메인루프 블로킹 방지)
                        _bc_daily_intents = await asyncio.to_thread(
                            bc_on_daily_close, snapshot, st, system_state)
                        intents += _bc_daily_intents
                        if _bc_daily_intents:
                            print(f"[BC] 일봉 시그널: {len(_bc_daily_intents)}건 진입 intent")
                    except Exception as _bc_e:
                        print(f"[BC] on_daily_close 오류(무시): {_bc_e}")

                # 매 틱 포지션 관리
                try:
                    _bc_tick_intents = bc_on_tick(snapshot, st)
                    intents += _bc_tick_intents
                except Exception as _bc_e2:
                    print(f"[BC] on_tick 오류(무시): {_bc_e2}")

            # ★ Crash Bounce 매 틱
            if _CB_ENABLED:
                try:
                    _cb_intents = cb_on_tick(snapshot, st)
                    intents += _cb_intents
                except Exception as _cb_e:
                    print(f"[CB] on_tick 오류(무시): {_cb_e}")

            # ★ V10.29: Counter 디버그 → 텔레그램 전송
            _ctr_msgs = system_state.pop("_counter_tg", [])
            if _ctr_msgs and _TELEGRAM_OK:
                try:
                    from telegram_engine import send_telegram_message
                    asyncio.ensure_future(send_telegram_message(
                        "\n".join(_ctr_msgs[-5:])))  # 최대 5개
                except Exception:
                    pass

            # ── 리스크 평가 ──────────────────────────────────────
            evaluated = []
            for intent in intents:
                evaluated_intent = evaluate_intent(
                    intent=intent,
                    snapshot=snapshot,
                    st=st,
                    cooldowns=cooldowns,
                    system_state=system_state,
                    dry_run=dry_run,
                )
                evaluated.append(evaluated_intent)

            # ── 실행 ─────────────────────────────────────────────
            results = await execute_intents(ex, evaluated, dry_run=dry_run, st=st)

            # ── 포지션 북 갱신 ───────────────────────────────────
            intents_map = {i.trace_id: i for i in evaluated}

            # [BUG-5 FIX] apply_order_results 이전에 pos_snap 캡처
            # ★ v10.10 fix: (symbol, side) 키로 저장 → 양방향 포지션 구분
            _pre_pos_snaps: dict = {}
            if _TELEGRAM_OK:
                for _s, _ss in st.items():
                    for _side, _p in iter_positions(_ss):
                        if _p:
                            _snap = dict(_p) if isinstance(_p, dict) else {
                                k: getattr(_p, k, None)
                                for k in ('ep', 'side', 'amt', 'dca_level', 'hedge_mode', 'role')
                            }
                            _pre_pos_snaps[(_s, _side)] = _snap
                            _pre_pos_snaps[_s] = _snap  # 레거시 호환

            apply_order_results(results, intents_map, st, cooldowns, snapshot)

            # ── v10.15b: 바이낸스 sync 매틱 복원 ──────────────────
            # (45초 reconcile → 신규 진입 인식 불가 문제로 되돌림)
            await _sync_positions_with_exchange(ex, st, snapshot)

            # ★ v10.24 Fix B: _manage_pending_limits 호출 추가
            # 정의만 되고 호출이 누락 → limit order 체결 추적/타임아웃 취소가 전혀 안 됨
            await _manage_pending_limits(ex, st, snapshot)

            # ★ V10.25: TP1 선주문 관리 (매 틱)
            await _manage_tp1_preorders(ex, st, snapshot, dry_run=dry_run)

            # ★ V10.28b: Trim 선주문 — DCA 체결 후 즉시 배치
            await _place_trim_preorders(ex, st, snapshot)

            # ★ V10.28b: Trim 선주문 취소 (포지션 청산 시)
            try:
                from v9.strategy.strategy_core import get_trim_cancel_queue
                from v9.execution.order_router import remove_pending_limit
                for _tcq in get_trim_cancel_queue():
                    _tc_oid = _tcq.get("oid", "")
                    _tc_sym = _tcq.get("sym", "")
                    if _tc_oid and _tc_sym:
                        try:
                            await asyncio.to_thread(ex.cancel_order, _tc_oid, _tc_sym)
                            print(f"[TRIM_CANCEL] {_tc_sym} oid={_tc_oid} 취소 완료")
                        except Exception as _tce:
                            print(f"[TRIM_CANCEL] {_tc_sym} oid={_tc_oid}: {_tce}")
                        remove_pending_limit(_tc_oid)
            except Exception:
                pass

            # ★ FIX-2: 유령 pending_entry 일괄 정리 (조건 확장)
            # 기존: 포지션+pending 동시 → 클리어
            # 추가: pending만 남은 경우도 클리어 (in-memory에 대응 주문 없으면 유령)
            try:
                from v9.execution.order_router import get_pending_limits as _gpl_ghost
                _live_oids = {str(info.get("order_id","")) for info in _gpl_ghost().values()}
            except Exception:
                _live_oids = set()
            for _pe_sym, _pe_ss in st.items():
                if not isinstance(_pe_ss, dict):
                    continue
                for _pe_side in ("buy", "sell"):
                    _pe_val = get_pending_entry(_pe_ss, _pe_side)
                    if _pe_val is None:
                        continue
                    # 케이스1: 포지션 있는데 pending도 있음 (기존 로직)
                    if get_p(_pe_ss, _pe_side) is not None:
                        set_pending_entry(_pe_ss, _pe_side, None)
                        continue
                    # 케이스2: 포지션 없고 pending만 있는데, in-memory 추적에 없음 → 유령
                    _pe_oid = str((_pe_val or {}).get("order_id", "")) if isinstance(_pe_val, dict) else ""
                    if _pe_oid and _pe_oid not in _live_oids:
                        set_pending_entry(_pe_ss, _pe_side, None)
                        print(f"[GHOST] {_pe_sym} {_pe_side} 유령 pending_entry 클리어 (oid={_pe_oid})")

            # ── [BUG-5 FIX] 체결 알림 ─────────────────────────
            if _TELEGRAM_OK:
                for _res in results:
                    if not _res.success:
                        continue
                    _intent_n = intents_map.get(_res.trace_id)
                    if _intent_n is None:
                        continue
                    # ★ v10.10 fix: (sym, side) 키 우선, 없으면 sym 키
                    _snap_key = (_res.symbol, _intent_n.side)
                    # TP1/CLOSE의 side는 청산 방향이므로 반대가 원래 포지션
                    if _intent_n.intent_type in (IntentType.TP1, IntentType.CLOSE,
                                                  IntentType.FORCE_CLOSE, IntentType.TRAIL_ON):
                        _pos_side = "sell" if _intent_n.side == "buy" else "buy"
                        _snap_key = (_res.symbol, _pos_side)
                    _pos_snap = _pre_pos_snaps.get(_snap_key) or _pre_pos_snaps.get(_res.symbol)
                    asyncio.ensure_future(
                        _notify_fill(
                            result=_res,
                            intent=_intent_n,
                            st=st,
                            snapshot=snapshot,
                            pos_snap=_pos_snap,
                        )
                    )

            # ── -2022 ReduceOnly 거절 처리 ────────────────────────
            for result in results:
                if not result.success and result.error:
                    err_str = str(result.error)
                    if "REDUCE_ONLY_REJECTED" in err_str or "-2022" in err_str:
                        sym_fail = result.symbol
                        ensure_slot(st, sym_fail)
                        # [BUG-3 FIX] 청산 인텐트 실패 → exit_fail_cooldown 300초
                        # open_fail 5초로 처리하면 반대방향 OPEN이 19초 후 열림
                        _intent_fail = intents_map.get(result.trace_id)
                        _itype_val = getattr(
                            getattr(_intent_fail, 'intent_type', None), 'value', ''
                        )
                        _exit_types = ('TRAIL_ON', 'FORCE_CLOSE', 'CLOSE', 'TP1', 'TP2')
                        if _itype_val in _exit_types:
                            st[sym_fail]['exit_fail_cooldown_until'] = now + 300
                            print(f"[V9 Runner] -2022 청산실패: {sym_fail} "
                                  f"exit_fail_cooldown 300초 ({_itype_val})")
                        else:
                            st[sym_fail]['open_fail_cooldown_until'] = now + 5
                            print(f"[V9 Runner] -2022 진입실패: {sym_fail} "
                                  f"open_fail_cooldown 5초")

            # ── 포지션 스냅샷 로그 (30초 주기) ──────────────────
            if now - last_save_ts >= 10:
                snapshot_positions(st, snapshot)
                # ★ v10.17: stage2 타이머 system_state에 저장 (재시작 복원용)
                try:
                    import v9.engines.hedge_core as _hc_sv
                    system_state['_skew_stage2_enter_ts'] = _hc_sv._skew_stage2_enter_ts
                except Exception:
                    pass
                _save_all(st, cooldowns, system_state)
                last_save_ts = now
                # ★ V10.28b: 일별 PnL 리포트 (00:05 UTC)
                await _daily_pnl_report(st)
                # ★ v10.17: 스큐 상태 로그 (log_skew.csv)
                try:
                    from v9.engines.hedge_core import (
                        calc_skew as _cs, _skew_stage2_enter_ts as _s2ts,
                        _is_hedge_required as _ihr,
                    )
                    from v9.logging.logger_csv import log_skew as _lskew
                    from v9.config import SKEW_STAGE2_TRIGGER as _s2t
                    _tc = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
                    if _tc > 0:
                        _sk, _lm, _sm = _cs(st, _tc)
                        _hvy = "buy" if _lm > _sm else "sell"
                        _hact = any(
                            isinstance(get_p(ss, s), dict)
                            and (get_p(ss, s) or {}).get("role") == "CORE_HEDGE"
                            for ss in st.values() if isinstance(ss, dict)
                            for s in ("buy", "sell")
                        )
                        _hreq = _ihr(st, snapshot, _sk, _hvy) if _sk >= _s2t else False
                        _s2m = (now - _s2ts) / 60 if _s2ts > 0 else 0.0
                        _lc = (2 if _sk >= _s2t else 1) if _sk >= 0.10 else 0
                        _lskew(
                            skew=_sk, long_mr=_lm, short_mr=_sm, heavy_side=_hvy,
                            lock_count=_lc, hedge_active=_hact, hedge_required=_hreq,
                            stage2_min=_s2m, mr=float(getattr(snapshot, "margin_ratio", 0) or 0),
                        )
                except Exception as _lse:
                    pass  # 로그 실패는 무시
                # ★ v10.15: minroi 30초마다 저장
                if now - _last_minroi_save_ts >= 30:
                    save_minroi(_minroi)
                    _last_minroi_save_ts = now

            # ── 슬롯 상태 출력 (디버그) ──────────────────────────
            slots = count_slots(st)
            print(
                f"[V9] {now_str()} | "
                f"bal={snapshot.real_balance_usdt:.2f} | "
                f"mr={mr:.3f} | "
                f"risk_slots={slots.risk_total}(L{slots.risk_long}/S{slots.risk_short}) | "
                f"hard_slots={slots.total} | "
                f"intents={len(intents)} approved={sum(1 for i in evaluated if i.approved)}"
            )

            # ── system_state.json 갱신 (텔레그램 봇 호환) ────────
            _write_system_state_compat(snapshot, system_state, st)

            # ★ V10.27f: log_skew CSV 기록 (30초 주기)
            if now - system_state.get("_last_skew_log_ts", 0) >= 30:
                try:
                    from v9.strategy.planners import _calc_urgency
                    from v9.engines.hedge_core import calc_skew, _skew_stage2_enter_ts
                    from v9.logging.logger_csv import log_skew
                    _total_cap_log = float(getattr(snapshot, "real_balance_usdt", 0) or 0)
                    if _total_cap_log > 0:
                        _sk, _lm, _sm = calc_skew(st, _total_cap_log)
                        _urg_log = _calc_urgency(st, snapshot)
                        _heavy = "buy" if _lm > _sm else "sell"
                        _mr_log = float(getattr(snapshot, "margin_ratio", 0) or 0)
                        _s2_min = (now - _skew_stage2_enter_ts) / 60 if _skew_stage2_enter_ts > 0 else 0
                        _lock_cnt = sum(1 for _s in st if isinstance(st.get(_s), dict)
                                        for _sd in ("buy", "sell")
                                        if isinstance(get_p(st[_s], _sd), dict)
                                        and get_p(st[_s], _sd).get("role") == "CORE_HEDGE")
                        log_skew(
                            skew=_sk, long_mr=_lm, short_mr=_sm,
                            heavy_side=_heavy, lock_count=_lock_cnt,
                            hedge_active=_lock_cnt > 0, hedge_required=_sk >= 0.15,
                            stage2_min=_s2_min, mr=_mr_log,
                            urgency=_urg_log["urgency"],
                            heavy_avg_roi=_urg_log["heavy_avg_roi"],
                        )
                        system_state["_urgency_score"] = _urg_log["urgency"]
                        system_state["_heavy_avg_roi"] = _urg_log["heavy_avg_roi"]
                except Exception as _skew_log_e:
                    print(f"[log_skew] 실패(무시): {_skew_log_e}")
                system_state["_last_skew_log_ts"] = now

        except Exception as e:
            consecutive_errors = system_state.get('_consecutive_errors', 0) + 1
            system_state['_consecutive_errors'] = consecutive_errors
            err_str = str(e)
            print(f"[V9 Runner] 루프 오류 ({consecutive_errors}회 연속): {err_str[:120]}")
            import traceback
            traceback.print_exc()

            # ── 네트워크/거래소 오류 → 즉시 재연결 ────────────────
            is_network_err = any(k in err_str for k in (
                'NetworkError', 'ExchangeNotAvailable', 'RequestTimeout',
                'ConnectionError', 'RemoteDisconnected', 'BrokenPipe',
                'TimeoutError', 'DDoSProtection', 'RateLimitExceeded',
            ))
            if is_network_err:
                print(f"[V9 Runner] 네트워크 오류 감지 → 거래소 재연결 시도")
                try:
                    await asyncio.to_thread(ex.close)
                except Exception as _cl_e:
                    print(f"[V9 Runner] exchange close 오류(무시): {_cl_e}")
                await asyncio.sleep(10)
                try:
                    ex = _make_exchange()
                    print(f"[V9 Runner] 재연결 성공")
                    system_state['_consecutive_errors'] = 0
                except Exception as re_err:
                    print(f"[V9 Runner] 재연결 실패: {re_err}")
                continue

            if consecutive_errors >= 3:
                print(f"[V9 Runner] ⚠ 연속 {consecutive_errors}회 오류 → 포지션 저장 후 5분 대기")
                try:
                    _save_all(st, cooldowns, system_state)
                except Exception as _sv_e:
                    print(f"[V9 Runner] 저장 오류(무시): {_sv_e}")
                system_state['_consecutive_errors'] = 0
                await asyncio.sleep(300)
            else:
                await asyncio.sleep(5)

        # ── 루프 주기 조절 (1초) ─────────────────────────────────
        system_state['_consecutive_errors'] = 0   # 정상 완료 시 카운터 리셋
        elapsed = time.time() - loop_start
        sleep_t = max(0.1, 1.0 - elapsed)
        await asyncio.sleep(sleep_t)


def run(dry_run: bool = True):
    """
    V9 Runner 진입점.
    main.py에서 호출:
        from v9.app.runner import run
        run(dry_run=True)
    """
    ex = _make_exchange()
    try:
        asyncio.run(_main_loop(ex, dry_run=dry_run))
    except KeyboardInterrupt:
        print("\n[V9 Runner] KeyboardInterrupt — 종료")
        os._exit(0)
    except Exception as e:
        print(f"\n[V9 Runner] FATAL: {e}")
        os._exit(1)
