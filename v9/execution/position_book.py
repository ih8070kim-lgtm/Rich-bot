"""
V9 Execution - Position Book  (v10.0 — hedge mode dual position)
포지션 상태 저장/로드/관리

[v10.0 구조 변경]
  HEDGE_MODE 지원: 심볼당 Long/Short 동시 보유
  - 기존: st[sym]["p"]  (단방향)
  - 신규: st[sym]["p_long"] / st[sym]["p_short"]  (양방향)

  헬퍼 함수:
    get_p(sym_st, side)      → 해당 방향 p 반환
    set_p(sym_st, side, p)   → 해당 방향 p 세팅
    is_active(sym_st)        → 어느 방향이든 포지션 있으면 True
    iter_positions(sym_st)   → (side, p) 튜플 이터레이터
    get_pending_entry(sym_st, side) → 해당 방향 pending_entry
    set_pending_entry(sym_st, side, v) → 해당 방향 pending_entry 세팅

  backward compat:
    _normalize_slot 에서 구 버전 'p'/'active' 필드를 새 구조로 마이그레이션
"""
import json
import os
import time

from v9.config import STATE_FILE, MINROI_FILE


# ── MinROI 상태 저장/로드 (v10.15) ──────────────────────────────

def load_minroi() -> dict:
    """minroi.json 로드 → {(sym, side): {worst_roi, worst_roi_t4, worst_roi_t5}}"""
    if not os.path.exists(MINROI_FILE):
        return {}
    try:
        with open(MINROI_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        # key: "SYM|side" → (sym, side)
        result = {}
        for k, v in raw.items():
            parts = k.split("|")
            if len(parts) == 2:
                result[(parts[0], parts[1])] = v
        return result
    except Exception as e:
        print(f"[minroi] load 실패: {e}")
        return {}


def save_minroi(data: dict):
    """minroi 상태 저장. data = {(sym, side): {worst_roi, ...}}"""
    try:
        serializable = {}
        for (sym, side), v in data.items():
            serializable[f"{sym}|{side}"] = v
        tmp = MINROI_FILE + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        os.replace(tmp, MINROI_FILE)
    except Exception as e:
        print(f"[minroi] save 실패: {e}")


def update_minroi(minroi: dict, sym: str, side: str, roi: float, dca_level: int):
    """매틱 minroi 갱신. 포지션별 worst_roi + tier별 worst 추적."""
    key = (sym, side)
    if key not in minroi:
        minroi[key] = {"worst_roi": 0.0, "worst_roi_t4": 0.0, "worst_roi_t5": 0.0}
    entry = minroi[key]
    if roi < entry.get("worst_roi", 0.0):
        entry["worst_roi"] = roi
    if dca_level >= 4 and roi < entry.get("worst_roi_t4", 0.0):
        entry["worst_roi_t4"] = roi
    if dca_level >= 5 and roi < entry.get("worst_roi_t5", 0.0):
        entry["worst_roi_t5"] = roi


# ── 슬롯 기본 구조 (v10.0) ───────────────────────────────────────
_SLOT_DEFAULTS = {
    'p_long':               None,   # Long 포지션 dict | None
    'p_short':              None,   # Short 포지션 dict | None
    'pending_entry_long':   None,
    'pending_entry_short':  None,
    'pending_exit':         None,
    'last_ohlcv_time':      0,
    'open_fail_cooldown_until':    0.0,
    'reduce_fail_cooldown_until':  0.0,
    'last_open_ts':         0.0,
    'cleared_at':           0.0,
    '_reconcile_miss':      0,
}


# ── 헬퍼 함수 ────────────────────────────────────────────────────

def get_p(sym_st: dict, side: str = None):
    """
    side 기반 포지션 dict 반환.
    side=None 이면 있는 것 우선 반환 (p_long > p_short).
    """
    if side == "buy":  return sym_st.get("p_long")
    if side == "sell": return sym_st.get("p_short")
    return sym_st.get("p_long") or sym_st.get("p_short")


def set_p(sym_st: dict, side: str, data) -> None:
    """해당 방향 포지션 세팅."""
    if side == "buy":  sym_st["p_long"]  = data
    else:              sym_st["p_short"] = data


def is_active(sym_st: dict) -> bool:
    """어느 방향이든 포지션이 있으면 True."""
    return sym_st.get("p_long") is not None or sym_st.get("p_short") is not None


def iter_positions(sym_st: dict):
    """심볼의 모든 활성 포지션 순회 → (side, p) 튜플."""
    if sym_st.get("p_long")  is not None: yield "buy",  sym_st["p_long"]
    if sym_st.get("p_short") is not None: yield "sell", sym_st["p_short"]


def get_pending_entry(sym_st: dict, side: str = None):
    """해당 방향 pending_entry 반환. side=None 이면 있는 것 반환."""
    if side == "buy":  return sym_st.get("pending_entry_long")
    if side == "sell": return sym_st.get("pending_entry_short")
    return sym_st.get("pending_entry_long") or sym_st.get("pending_entry_short")


def set_pending_entry(sym_st: dict, side: str, data) -> None:
    """해당 방향 pending_entry 세팅."""
    if side == "buy":  sym_st["pending_entry_long"]  = data
    else:              sym_st["pending_entry_short"] = data


def _normalize_slot(slot: dict) -> dict:
    """
    슬롯 필드 정규화.
    구버전 'p'/'active' 필드를 새 구조로 마이그레이션.
    """
    # ── backward compat: 구 버전 p/active → p_long/p_short ──────
    old_p = slot.pop("p", None)
    slot.pop("active", None)
    old_pe = slot.pop("pending_entry", None)

    if old_p and isinstance(old_p, dict):
        side = old_p.get("side", "buy")
        key  = "p_long" if side == "buy" else "p_short"
        if slot.get(key) is None:
            slot[key] = old_p

    if old_pe and isinstance(old_pe, dict):
        pe_side = old_pe.get("side", "buy")
        pe_key  = "pending_entry_long" if pe_side == "buy" else "pending_entry_short"
        if slot.get(pe_key) is None:
            slot[pe_key] = old_pe

    # 누락 필드 기본값 보완
    for k, v in _SLOT_DEFAULTS.items():
        if k not in slot:
            slot[k] = v
    return slot


def load_position_book() -> dict:
    """
    포지션 북 로드 (v9_state.json)
    ★ v10.6: main 파일 파싱 실패 시 .bak에서 복원
    반환: {'st': {...}, 'cooldowns': {...}, 'system_state': {...}}
    """
    default = {
        'st': {},
        'cooldowns': {},
        'system_state': {
            'shutdown_active': False,
            'shutdown_until': 0.0,
            'shutdown_reason': '',
            'use_long': True,
            'use_short': True,
            'is_locked': False,
            'baseline_balance': 0.0,
            'baseline_date': '',
            'initial_balance': 0.0,
            'utilization_rate': 1.0,
            'corr_guard_last_ts': 0.0,
            'corr_guard_breach_ts': {},
        }
    }

    def _try_load(path: str) -> dict | None:
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding='utf-8') as f:
                data = json.load(f)
            for k, v in default.items():
                if k not in data:
                    data[k] = v
            if 'system_state' not in data:
                data['system_state'] = dict(default['system_state'])
            else:
                for k, v in default['system_state'].items():
                    if k not in data['system_state']:
                        data['system_state'][k] = v
            for sym in data.get('st', {}):
                if isinstance(data['st'][sym], dict):
                    data['st'][sym] = _normalize_slot(data['st'][sym])
            return data
        except Exception as e:
            print(f"[position_book] {path} 파싱 실패: {e}")
            return None

    # 1차: main 파일
    result = _try_load(STATE_FILE)
    if result is not None:
        return result

    # 2차: .bak 파일 복원 시도
    bak_path = STATE_FILE + ".bak"
    result = _try_load(bak_path)
    if result is not None:
        print(f"[position_book] ⚠ main 파일 손상 → .bak에서 복원 성공")
        # 복원된 데이터로 main 파일 즉시 재저장
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return result

    print(f"[position_book] ⚠ main + .bak 모두 실패 → 기본값 반환")
    return default


def save_position_book(st: dict, cooldowns: dict, system_state: dict):
    """
    포지션 북 저장 (원자적 write + Windows PermissionError 방어)
    ★ v10.6: 기존 파일을 .bak으로 보존 후 쓰기
    """
    try:
        import shutil
        tmp = STATE_FILE + ".tmp"
        bak = STATE_FILE + ".bak"
        d = os.path.dirname(os.path.abspath(STATE_FILE))
        if d:
            os.makedirs(d, exist_ok=True)
        # ★ 다운타임 감지용: 마지막 저장 시각 기록
        system_state['_last_save_ts'] = time.time()
        data = {
            'st': st,
            'cooldowns': cooldowns,
            'system_state': system_state,
        }
        # .bak 보존 (기존 정상 파일 백업)
        if os.path.exists(STATE_FILE):
            try:
                shutil.copy2(STATE_FILE, bak)
            except Exception as _bak_e:
                print(f"[position_book] .bak 백업 실패(무시): {_bak_e}")
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.replace(tmp, STATE_FILE)
        except PermissionError:
            try:
                with open(STATE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    except Exception as e:
        print(f"[position_book] save 실패: {e}")


def clear_position(st: dict, symbol: str, side: str = None):
    """
    심볼 포지션 초기화.
    side 지정 시 해당 방향만 클리어 (hedge mode).
    side=None 이면 양방향 모두 클리어.
    """
    if symbol not in st:
        return
    now = time.time()
    sym_st = st[symbol]

    if side in ("buy", None):
        sym_st["p_long"]             = None
        sym_st["pending_entry_long"] = None
    if side in ("sell", None):
        sym_st["p_short"]            = None
        sym_st["pending_entry_short"]= None

    # 양방향 모두 비었을 때만 공통 필드 초기화
    if sym_st.get("p_long") is None and sym_st.get("p_short") is None:
        sym_st["pending_exit"] = None
        sym_st["cleared_at"]   = now
        sym_st["_reconcile_miss"] = 0


def ensure_slot(st: dict, symbol: str):
    """심볼 슬롯이 없으면 기본값으로 생성."""
    if symbol not in st:
        st[symbol] = dict(_SLOT_DEFAULTS)
    else:
        _normalize_slot(st[symbol])
