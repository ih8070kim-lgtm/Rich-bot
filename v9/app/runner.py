"""
V9 App Runner
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
    LEVERAGE,
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
    _TELEGRAM_OK = True
except Exception as _tg_err:
    print(f"[V9 Runner] telegram_engine import 실패 (알림 비활성): {_tg_err}")
    _TELEGRAM_OK = False
from v9.engines.dca_engine import generate_dca_intents
from v9.risk.risk_manager import generate_corrguard_intents


# ═══════════════════════════════════════════════════════════════
# v10.11b: 바이낸스 ↔ 포지션북 동기화
# DCA 체결이 포지션북에 미반영되는 버그 방어
# ═══════════════════════════════════════════════════════════════
_last_sync_ts = 0.0
_SYNC_INTERVAL = 30  # 초

async def _sync_positions_with_exchange(ex, st):
    """바이낸스 실제 포지션과 포지션북 비교, 불일치 시 바이낸스 기준 반영."""
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
                    # ★ v10.12: qty 증가 시 dca_level 역추정
                    if ex_qty > book_qty * 1.05:
                        from v9.config import DCA_WEIGHTS as _DW, LEVERAGE as _LV, TOTAL_MAX_SLOTS as _TS
                        _cur_dca = int(book_p.get('dca_level', 1) or 1)
                        _bal_est = float(getattr(snapshot, 'real_balance_usdt', 4000) or 4000) if 'snapshot' in dir() else 4000
                        _grid_est = (_bal_est / _TS) * _LV
                        _tw = sum(_DW)
                        _notional = ex_qty * (ex_ep if ex_ep > 0 else old_ep)
                        _cum = 0; _est_dca = 1
                        for _wi in range(len(_DW)):
                            _cum += _DW[_wi] / _tw
                            if _notional <= _grid_est * _cum * 1.15:
                                _est_dca = _wi + 1; break
                            _est_dca = _wi + 1
                        _est_dca = min(_est_dca, 5)
                        if _est_dca > _cur_dca:
                            print(f"[SYNC] ★ {sym} {side} dca_level 역추정: "
                                  f"{_cur_dca}→{_est_dca} (notional=${_notional:.0f} grid=${_grid_est:.0f})")
                            book_p['dca_level'] = _est_dca
                            if _est_dca >= 5:
                                book_p['max_dca_reached'] = True
                if ep_diff and ex_ep > 0:
                    book_p['ep'] = ex_ep
                _what = []
                if qty_diff: _what.append(f"qty:{old_qty:.1f}→{ex_qty:.1f}")
                if ep_diff:  _what.append(f"ep:{old_ep:.6f}→{ex_ep:.6f}")
                print(f"[SYNC] ★ {sym} {side} 수정: {' | '.join(_what)}")
        else:
            # ★ v10.11b: 포지션북에 없는데 바이낸스에 있음 → 자동 복구
            # DCA 미반영으로 포지션북이 삭제된 경우 방어
            print(f"[SYNC] ★ {sym} {side} 고아 포지션 복구: "
                  f"qty={ex_qty:.1f} ep={ex_ep:.4f} (바이낸스 기준)")
            ensure_slot(st, sym)
            sym_st = st[sym]
            set_p(sym_st, side, {
                "symbol": sym, "side": side,
                "ep": ex_ep, "original_ep": ex_ep,
                "amt": ex_qty,
                "time": now, "last_dca_time": now,
                "atr": 0.0, "tag": "V9_RECOVERED",
                "step": 0, "dca_level": 1,
                "dca_targets": [],
                "max_roi_seen": 0.0, "pending_dca": None,
                "trailing_on_time": None,
                "hedge_mode": False,
                "open_cooldown_until": now,
                "tp1_done": False, "tp2_done": False,
                "entry_type": "MR", "role": "CORE_MR",
                "source_sym": "", "asym_forced": False,
                "last_hedge_exit_p": 0.0,
                "last_hedge_exit_side": "",
                "hedge_rolling_count": 0,
                "source_sl_orphan": False,
                "locked_regime": "LOW",
                "hedge_entry_price": 0.0,
                "t5_entry_price": 0.0,
                "sh_trigger": False,
                "insurance_timecut": 0,
            })

    # ── 2) 포지션북에 있는데 바이낸스에 없음 → 유령 포지션 제거 ──
    for (sym, side), book_p in book_pos.items():
        if (sym, side) not in ex_pos:
            book_qty = float(book_p.get('amt', 0) or 0)
            if book_qty > 0:
                print(f"[SYNC] ★ {sym} {side} 유령 포지션 제거: "
                      f"qty={book_qty:.1f} (바이낸스에 없음)")
                sym_st = st.get(sym, {})
                set_p(sym_st, side, None)


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
        }

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


def _trim_ohlcv_pool(snapshot) -> None:
    """
    ohlcv_pool 메모리 누수 방지.
    1m: 최대 300개, 15m: 최대 150개 유지 (planners 최대 필요: 1m 65개, 15m 15개)
    """
    pool = getattr(snapshot, 'ohlcv_pool', None)
    if not pool:
        return
    MAX_1M  = 300
    MAX_5M  = 100   # [추가-2 FIX] 5m 50봉 × 2배 여유
    MAX_15M = 150
    MAX_1H  = 50    # [추가-2 FIX] 1h 25봉 × 2배 여유
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


def _normalize_symbol(raw: str) -> str:
    """
    거래소 심볼 → 내부 심볼 정규화.
    XRP/USDT:USDT → XRP/USDT
    XRPUSDT       → XRP/USDT  (CCXT id 형태)
    BTC/USDT      → BTC/USDT  (이미 정규화된 경우 pass-through)
    """
    if not raw:
        return raw
    # ':USDT' 제거 (CCXT unified perpetual 포맷)
    s = raw.replace(':USDT', '').replace(':USD', '')
    # 이미 '/' 포함 → 정규화 완료
    if '/' in s:
        return s
    # XRPUSDT 형태 → XRP/USDT
    if s.endswith('USDT'):
        base = s[:-4]
        return f"{base}/USDT"
    return s


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


async def _main_loop(ex_init, dry_run: bool):
    """V9 메인 루프"""
    print(f"[V9 Runner] 시작 (dry_run={dry_run})")
    ex = ex_init  # 재연결 시 교체 가능

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
            save_position_book(st, cooldowns, system_state)
        else:
            print(f"[STARTUP] 미체결 주문 없음")
    except Exception as _startup_e:
        print(f"[STARTUP] 미체결 주문 정리 실패(무시): {_startup_e}")

    while True:
        now = time.time()
        loop_start = now

        try:
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
                    save_position_book(st, cooldowns, system_state)

            # ── 셧다운 만료 체크 ─────────────────────────────────
            if system_state.get('shutdown_active', False):
                if now >= system_state.get('shutdown_until', 0.0):
                    system_state['shutdown_active'] = False
                    system_state['shutdown_reason'] = ''
                    print("[V9 Runner] 셧다운 만료 → 정상 복귀")
                    save_position_book(st, cooldowns, system_state)

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
                    save_position_book(st, cooldowns, system_state)
                    print(f"[V9] 텔레그램 전체 청산: {len(_close_intents)}건 ({_close_mode})")
                    continue

            # ★ v10.6: 현재 레짐 기록 (텔레그램 봇 표시용)
            try:
                from v9.strategy.planners import _btc_vol_regime
                system_state["_current_regime"] = _btc_vol_regime(snapshot)
            except Exception:
                pass

            # ── Intent 생성 ──────────────────────────────────────
            intents = generate_all_intents(snapshot, st, cooldowns, system_state)
            intents += generate_dca_intents(snapshot, st, cooldowns)
            intents += generate_corrguard_intents(snapshot, st, system_state)

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

            # ── v10.11b: 바이낸스 ↔ 포지션북 동기화 ──────────────
            await _sync_positions_with_exchange(ex, st)

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
                save_position_book(st, cooldowns, system_state)
                last_save_ts = now

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
                    save_position_book(st, cooldowns, system_state)
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
