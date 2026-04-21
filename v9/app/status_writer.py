"""
★ V10.29e: 라이브 대시보드용 상태 JSON 작성기
runner 메인 루프에서 매 틱 호출 → v9_status.json 갱신
status_server.py가 이 파일을 HTTP로 서빙
"""
import json
import os
import time
from datetime import datetime

# ★ V10.31c: try 블록 내부 import → module-level 승격
# 함수 내 조건부 import는 UnboundLocalError/NameError 위험
from v9.execution.position_book import iter_positions
from v9.utils.utils_math import calc_roi_pct
from v9.config import LEVERAGE


# ★ V10.31c: 경로 버그 수정 — status_server.py(프로젝트 루트)와 파일 경로 일치
# 수정 전: _BASE_DIR = v9/ → v9/v9_status.json (status_server는 프로젝트 루트를 봄)
# 수정 후: _BASE_DIR = 프로젝트 루트 → v9_status.json (일치)
_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
_STATUS_PATH = os.path.join(_BASE_DIR, "v9_status.json")
_LOG_DIR = os.path.join(_BASE_DIR, "v9_logs")
_BAL_HISTORY = os.path.join(_LOG_DIR, "log_balance.csv")
_LAST_WRITE = 0.0
_LAST_BAL_WRITE = 0.0
_WRITE_INTERVAL = 3.0     # status JSON 갱신 주기
_BAL_INTERVAL = 60.0       # 잔고 기록 주기 (1분)


def _tail_lines(filepath: str, n: int = 30) -> list:
    """파일 끝에서 n줄만 읽음. 파일 크기 무관하게 O(n)."""
    try:
        with open(filepath, 'rb') as f:
            f.seek(0, 2)  # EOF
            fsize = f.tell()
            if fsize == 0:
                return []
            # 줄당 평균 200바이트 가정, 넉넉히 2배
            chunk = min(fsize, n * 400)
            f.seek(max(0, fsize - chunk))
            data = f.read().decode('utf-8', errors='replace')
            lines = data.splitlines()
            return lines[-n:]
    except Exception:
        return []


def _parse_trade_line(line: str):
    """trade CSV 한 줄 → dict. 실패 시 None."""
    cols = line.strip().split(",")
    if len(cols) < 17 or not cols[0].startswith("2026"):
        return None
    reason = cols[11]
    if reason in ("", "GHOST_CLEANUP"):
        return None
    try:
        return {
            "time": cols[0][5:16],
            "date": cols[0][:10],
            "sym": cols[2].replace("/USDT", ""),
            "side": cols[3],
            "pnl": round(float(cols[7] or 0), 2),
            "roi": round(float(cols[8] or 0), 2),
            "tier": cols[9],
            "reason": reason,
            "role": cols[16] if len(cols) > 16 else "",
        }
    except (ValueError, IndexError):
        return None


def _compute_perf_metrics(bal_history_full: list) -> dict:
    """★ V10.31d: MDD / Sharpe / CAGR 계산.

    입력: [{t: "MM-DD HH:MM", b: balance}, ...] — 1분 간격
    전략: 일별 마감 잔고 추출 → daily return → MDD / Sharpe.

    **경고**: n<30일이면 Sharpe 통계적 의미 거의 없음. 표본 수 함께 리턴.
    """
    result = {
        "mdd_pct": 0.0,          # 최대 낙폭 % (음수)
        "mdd_abs": 0.0,          # 최대 낙폭 $
        "sharpe": 0.0,           # annualized Sharpe (rf=0)
        "n_days": 0,             # 일별 샘플 수
        "total_return_pct": 0.0, # 누적 수익률 %
        "warning": "",           # 신뢰도 경고
    }
    if not bal_history_full or len(bal_history_full) < 2:
        result["warning"] = "데이터 부족"
        return result

    # ── 일별 마감 잔고 추출 (같은 날짜의 마지막 레코드) ──
    daily_close = {}  # date -> balance
    for rec in bal_history_full:
        t = rec.get("t", "")
        if len(t) < 5:
            continue
        date_key = t[:5]  # "MM-DD"
        daily_close[date_key] = rec.get("b", 0.0)

    if len(daily_close) < 2:
        result["warning"] = "데이터 부족 (일별 샘플 1개)"
        return result

    dates = sorted(daily_close.keys())
    closes = [daily_close[d] for d in dates if daily_close[d] > 0]
    n = len(closes)
    result["n_days"] = n

    if n < 2:
        result["warning"] = "유효 잔고 <2"
        return result

    # ── MDD ──
    peak = closes[0]
    max_dd_pct = 0.0
    max_dd_abs = 0.0
    for b in closes:
        if b > peak:
            peak = b
        dd_abs = b - peak
        dd_pct = dd_abs / peak if peak > 0 else 0.0
        if dd_pct < max_dd_pct:
            max_dd_pct = dd_pct
            max_dd_abs = dd_abs
    result["mdd_pct"] = round(max_dd_pct * 100, 2)
    result["mdd_abs"] = round(max_dd_abs, 2)

    # ── 누적 수익률 ──
    result["total_return_pct"] = round((closes[-1] / closes[0] - 1) * 100, 2)

    # ── Sharpe (일별 단순 수익률 → annualized) ──
    rets = []
    for i in range(1, n):
        if closes[i - 1] > 0:
            rets.append(closes[i] / closes[i - 1] - 1)
    if len(rets) < 2:
        result["warning"] = f"n={n}일, 수익률 샘플 부족"
        return result
    mean_r = sum(rets) / len(rets)
    var_r = sum((r - mean_r) ** 2 for r in rets) / (len(rets) - 1)
    std_r = var_r ** 0.5
    if std_r > 0:
        # 365일 연율화 (crypto 24/7)
        sharpe = (mean_r / std_r) * (365 ** 0.5)
        result["sharpe"] = round(sharpe, 2)

    # ── 신뢰도 경고 ──
    if n < 7:
        result["warning"] = f"n={n}일 — 통계 무의미 (최소 30일 권장)"
    elif n < 30:
        result["warning"] = f"n={n}일 — 신뢰도 낮음 (최소 30일 권장)"
    elif n < 90:
        result["warning"] = f"n={n}일 — 참고용 (90일+ 권장)"

    # ★ V10.31d 핫픽스: NaN/Inf 방어 — JSON 직렬화 실패 방지
    import math
    for _k in ("mdd_pct", "mdd_abs", "sharpe", "total_return_pct"):
        _v = result.get(_k, 0.0)
        if not isinstance(_v, (int, float)) or math.isnan(_v) or math.isinf(_v):
            result[_k] = 0.0
    return result


def _record_balance(bal: float):
    """1분마다 잔고를 CSV에 기록 (equity curve용)."""
    global _LAST_BAL_WRITE
    now = time.time()
    if now - _LAST_BAL_WRITE < _BAL_INTERVAL or bal <= 0:
        return
    _LAST_BAL_WRITE = now
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        with open(_BAL_HISTORY, 'a', encoding='utf-8') as f:
            f.write(f"{ts},{bal:.2f}\n")
    except Exception:
        pass


def _build_ptp_status(system_state: dict, current_balance: float) -> dict:
    """★ V10.31n: PTP 상태 요약 — 대시보드 표시용.
    
    상태 구분:
      - idle:    peak_gain < 1% (arming 안 됨)
      - armed:   peak_gain ≥ 1% + drop 미달 (대기 중)
      - active:  trigger 중 (단계 진행 중)
    """
    result = {
        "state": "idle",
        "session_start": 0.0,
        "peak": 0.0,
        "peak_gain_pct": 0.0,
        "current_drop_pct": 0.0,
        "drop_thresh_pct": 0.0,
        "last_step": -1,
    }
    if not system_state or current_balance <= 0:
        return result
    
    session_start = float(system_state.get("_ptp_session_start", 0) or 0)
    peak = float(system_state.get("_ptp_peak_balance", 0) or 0)
    if session_start <= 0 or peak <= 0:
        return result
    
    peak_gain_pct = (peak - session_start) / session_start * 100.0
    current_drop_pct = (peak - current_balance) / session_start * 100.0
    
    result["session_start"] = round(session_start, 2)
    result["peak"] = round(peak, 2)
    result["peak_gain_pct"] = round(peak_gain_pct, 3)
    result["current_drop_pct"] = round(current_drop_pct, 3)
    
    # tiered drop 임계 계산
    try:
        from v9.config import _ptp_get_drop_thresh
        _dt = _ptp_get_drop_thresh(peak_gain_pct)
        if _dt is not None:
            result["drop_thresh_pct"] = round(_dt, 3)
    except Exception:
        pass
    
    # 상태 분류
    if system_state.get("_ptp_trigger_ts"):
        result["state"] = "active"
        result["last_step"] = int(system_state.get("_ptp_last_step", -1))
    elif peak_gain_pct >= 1.0:
        result["state"] = "armed"
    
    return result


def write_status(st: dict, snapshot, system_state: dict, cooldowns: dict):
    """runner 메인 루프에서 호출. 대시보드용 요약 JSON 작성."""
    global _LAST_WRITE
    now = time.time()
    if now - _LAST_WRITE < _WRITE_INTERVAL:
        return
    _LAST_WRITE = now

    try:
        # ★ V10.31c: 위 3개 import는 module-level로 이동됨

        bal = float(getattr(snapshot, 'real_balance_usdt', 0) or 0) if snapshot else 0
        mr = float(getattr(snapshot, 'margin_ratio', 0) or 0) if snapshot else 0
        prices = (snapshot.all_prices or {}) if snapshot else {}

        # ── 잔고 기록 (1분마다) ──
        _record_balance(bal)

        # ── 포지션 수집 ──
        positions = []
        total_unrealized = 0.0
        long_count = short_count = 0
        long_notional = short_notional = 0.0

        for sym, sym_st in st.items():
            if not isinstance(sym_st, dict):
                continue
            for side, p in iter_positions(sym_st):
                if not isinstance(p, dict):
                    continue
                ep = float(p.get("ep", 0) or 0)
                amt = float(p.get("amt", 0) or 0)
                cp = float(prices.get(sym, 0) or 0)
                dca = int(p.get("dca_level", 1) or 1)
                role = p.get("role", "CORE_MR")
                step = int(p.get("step", 0) or 0)
                max_roi = float(p.get("max_roi_seen", 0) or 0)
                worst_roi = float(p.get("worst_roi", 0) or 0)
                entry_type = p.get("entry_type", "MR")

                roi = calc_roi_pct(ep, cp, side, LEVERAGE) if ep > 0 and cp > 0 else 0.0
                notional = amt * cp if cp > 0 else amt * ep
                pnl_est = notional * roi / 100 / LEVERAGE if notional > 0 else 0.0

                if side == "buy":
                    long_count += 1
                    long_notional += notional
                else:
                    short_count += 1
                    short_notional += notional

                total_unrealized += pnl_est

                # ★ V10.31c: 코어(MR/TREND) vs 보조(BC/CB) 분리
                is_core = role not in ("BC", "CB")

                positions.append({
                    "sym": sym.replace("/USDT", ""),
                    "side": "LONG" if side == "buy" else "SHORT",
                    "tier": dca,
                    "roi": round(roi, 2),
                    "pnl": round(pnl_est, 2),
                    "ep": round(ep, 6),
                    "cp": round(cp, 6),
                    "notional": round(notional, 1),
                    "max_roi": round(max_roi, 2),
                    "worst_roi": round(worst_roi, 2),
                    "role": role,
                    "step": step,
                    "entry_type": entry_type,
                    "hold_min": round((now - float(p.get("time", now) or now)) / 60, 0),
                    "is_core": is_core,  # ★ V10.31c
                })

        # ── 스큐/어전시 ──
        skew_data = {}
        try:
            from v9.strategy.planners import _calc_urgency
            urg = _calc_urgency(st, snapshot)
            skew_data = {
                "urgency": round(urg.get("urgency", 0), 1),
                "skew_pct": round(urg.get("skew_pct", 0), 1),
                "heavy_side": urg.get("heavy_side", ""),
                "heavy_roi": round(urg.get("heavy_roi", 0), 1),
            }
        except Exception:
            pass

        # ── 최근 트레이드 (마지막 30줄만 읽음) ──
        recent_trades = []
        trades_file = os.path.join(_LOG_DIR, "log_trades.csv")
        tail = _tail_lines(trades_file, 30)
        for line in tail:
            t = _parse_trade_line(line)
            if t:
                recent_trades.append(t)

        # ── 오늘 PnL (마지막 300줄에서 집계 — 하루 최대 ~80건) ──
        today_pnl = 0.0
        today_trades = 0
        today_wins = 0
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        tail_today = _tail_lines(trades_file, 300)
        for line in tail_today:
            t = _parse_trade_line(line)
            if t and t["date"] == today_str:
                today_pnl += t["pnl"]
                today_trades += 1
                if t["pnl"] > 0:
                    today_wins += 1

        # ── ★ V10.31c: "오늘" 탭용 — 최근 7일 일별 + 전략별 + 시간대별 ──
        daily_list = []
        strat_pnl = {"MR": 0.0, "TREND": 0.0, "BC": 0.0, "CB": 0.0, "OTHER": 0.0}
        hour_pnl = [0.0] * 24  # 오늘 시간대별 (UTC)
        try:
            tail_week = _tail_lines(trades_file, 2000)
            _daily_map = {}
            for line in tail_week:
                t = _parse_trade_line(line)
                if not t:
                    continue
                d = t["date"]
                if d not in _daily_map:
                    _daily_map[d] = {"pnl": 0.0, "trades": 0, "wins": 0}
                _daily_map[d]["pnl"]    += t["pnl"]
                _daily_map[d]["trades"] += 1
                if t["pnl"] > 0:
                    _daily_map[d]["wins"] += 1
                # 전략별 분류 (role 기반)
                role = (t.get("role", "") or "").upper()
                if role == "BC":
                    strat_pnl["BC"] += t["pnl"]
                elif role == "CB":
                    strat_pnl["CB"] += t["pnl"]
                elif role in ("CORE_MR", "CORE_BREAKOUT", ""):
                    # entry_type 있으면 TREND 분리, 없으면 MR
                    # log_trades에 entry_type 칼럼이 없으므로 기본 MR로
                    strat_pnl["MR"] += t["pnl"]
                else:
                    strat_pnl["OTHER"] += t["pnl"]
                # 시간대별 (오늘만)
                if d == today_str:
                    try:
                        hh = int(t["time"][6:8])  # "MM-DD HH:MM" → HH
                        if 0 <= hh < 24:
                            hour_pnl[hh] += t["pnl"]
                    except Exception:
                        pass
            # 최근 7일만 정렬
            _sorted_dates = sorted(_daily_map.keys())[-7:]
            for d in _sorted_dates:
                dd = _daily_map[d]
                daily_list.append({
                    "date": d[5:],  # MM-DD
                    "pnl": round(dd["pnl"], 2),
                    "trades": dd["trades"],
                    "wins": dd["wins"],
                    "wr": round(dd["wins"] / dd["trades"] * 100, 0) if dd["trades"] else 0,
                })
            strat_pnl = {k: round(v, 2) for k, v in strat_pnl.items()}
        except Exception:
            pass

        # ── ★ V10.31c: "인사이트" 탭용 — 규칙 기반 경고/관찰 ──
        insights = []
        try:
            core_pos = [p for p in positions if p.get("is_core", True)]
            t3_longs  = [p for p in core_pos if p["side"] == "LONG"  and p["tier"] >= 3]
            t3_shorts = [p for p in core_pos if p["side"] == "SHORT" and p["tier"] >= 3]
            t3_count = len(t3_longs) + len(t3_shorts)
            # 규칙 1: T3 포화
            if len(t3_longs) >= 4:
                insights.append({"level": "warn", "text": f"롱 T3 {len(t3_longs)}/4 포화 — 추가 롱 진입 차단됨"})
            if len(t3_shorts) >= 4:
                insights.append({"level": "warn", "text": f"숏 T3 {len(t3_shorts)}/4 포화 — 추가 숏 진입 차단됨"})
            # 규칙 2: L/S notional 편중
            if long_notional > 0 and short_notional > 0:
                ratio = max(long_notional, short_notional) / min(long_notional, short_notional)
                if ratio >= 2.5:
                    heavy = "롱" if long_notional > short_notional else "숏"
                    insights.append({"level": "warn",
                        "text": f"{heavy} 편중 {ratio:.1f}:1 (L ${long_notional:.0f} / S ${short_notional:.0f})"})
            # 규칙 3: URGENCY
            urg = skew_data.get("urgency", 0)
            if urg >= 30:
                insights.append({"level": "warn", "text": f"URGENCY {urg:.0f} — 고위험 구간"})
            elif urg >= 15:
                insights.append({"level": "info", "text": f"URGENCY {urg:.0f} — 주의"})
            # 규칙 4: 실현 vs 미실현 갭
            net_today = today_pnl + total_unrealized
            if today_pnl > 10 and total_unrealized < -today_pnl:
                insights.append({"level": "warn",
                    "text": f"실현 +${today_pnl:.0f} 이나 미실현 ${total_unrealized:+.0f} → 실손익 ${net_today:+.0f}"})
            # 규칙 5: 깊게 물린 T3
            for p in sorted(t3_longs + t3_shorts, key=lambda x: x["roi"])[:3]:
                if p["roi"] <= -6:
                    insights.append({"level": "info",
                        "text": f"{p['sym']} {p['side']} T3 {p['roi']:+.1f}% — HARD_SL 접근"})
            # 규칙 6: 마진 비율
            if mr >= 0.80:
                insights.append({"level": "crit", "text": f"MR {mr*100:.1f}% — Killswitch 발동 임박"})
            elif mr >= 0.65:
                insights.append({"level": "warn", "text": f"MR {mr*100:.1f}% — 마진 관리 주의"})
            # 규칙 7: 코어/보조 포지션 요약
            sub_pos = [p for p in positions if not p.get("is_core", True)]
            if sub_pos:
                sub_pnl = sum(p["pnl"] for p in sub_pos)
                insights.append({"level": "info",
                    "text": f"보조(BC/CB) {len(sub_pos)}건 미실현 ${sub_pnl:+.1f}"})
            # 긍정 신호도 추가
            if today_wins >= 10 and today_wins / max(today_trades, 1) >= 0.9:
                insights.append({"level": "good",
                    "text": f"오늘 WR {today_wins}/{today_trades} ({today_wins/today_trades*100:.0f}%) 유지 중"})
        except Exception:
            pass

        # ── 잔고 히스토리 (최근 24h = 1440줄) ──
        bal_history = []
        if os.path.exists(_BAL_HISTORY):
            blines = _tail_lines(_BAL_HISTORY, 1440)
            for bl in blines:
                parts = bl.strip().split(",")
                if len(parts) == 2:
                    try:
                        bal_history.append({
                            "t": parts[0][5:],  # MM-DD HH:MM
                            "b": round(float(parts[1]), 1),
                        })
                    except ValueError:
                        pass

        # ── ★ V10.31d: MDD / Sharpe 계산 (전체 잔고 이력 기반) ──
        # 잔고 이력을 전체 스캔 (최대 30000줄 ≈ 20일 분). 일별 마감으로 압축해 계산
        perf = {"mdd_pct": 0.0, "mdd_abs": 0.0, "sharpe": 0.0,
                "n_days": 0, "total_return_pct": 0.0, "warning": ""}
        try:
            bal_full = []
            if os.path.exists(_BAL_HISTORY):
                for bl in _tail_lines(_BAL_HISTORY, 30000):
                    parts = bl.strip().split(",")
                    if len(parts) == 2:
                        try:
                            bal_full.append({
                                "t": parts[0][5:],
                                "b": float(parts[1]),
                            })
                        except ValueError:
                            pass
            perf = _compute_perf_metrics(bal_full)
        except Exception:
            pass

        # ── ★ V10.31d: 수수료 / 펀딩비 7일 누계 ──
        fee_7d = 0.0
        funding_7d = 0.0
        try:
            # trades.csv 19번째 컬럼(index 18) = fee_usdt (V10.31d 이후 파일만)
            tail_fee = _tail_lines(trades_file, 2000)
            for line in tail_fee:
                cols = line.strip().split(",")
                if len(cols) >= 19 and cols[0].startswith("2026"):
                    try:
                        fee_7d += float(cols[18] or 0)
                    except (ValueError, IndexError):
                        pass
            # funding.csv
            funding_file = os.path.join(_LOG_DIR, "log_funding.csv")
            if os.path.exists(funding_file):
                # 최근 7일치 추출
                import datetime as _dt
                _cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=7)
                for fl in _tail_lines(funding_file, 10000):
                    parts = fl.strip().split(",")
                    if len(parts) >= 3 and parts[0].startswith("2026"):
                        try:
                            _t = _dt.datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
                            if _t >= _cutoff:
                                funding_7d += float(parts[2] or 0)
                        except (ValueError, IndexError):
                            pass
        except Exception:
            pass

        # MDD/Sharpe 기반 추가 인사이트
        try:
            if perf.get("mdd_pct", 0) <= -5:
                insights.append({"level": "warn",
                    "text": f"MDD {perf['mdd_pct']}% — 낙폭 주시"})
            if perf.get("warning"):
                insights.append({"level": "info",
                    "text": f"지표 신뢰도: {perf['warning']}"})
            # 수수료·펀딩 누수 경고
            total_today_7d = sum(d.get("pnl", 0) for d in daily_list)
            total_cost = abs(fee_7d) + abs(min(funding_7d, 0))  # funding 지불만
            if total_today_7d != 0 and total_cost > abs(total_today_7d) * 0.3:
                insights.append({"level": "warn",
                    "text": f"7d 비용(수수료+펀딩지불) ${total_cost:.1f} "
                            f"vs 실현 ${total_today_7d:+.1f} — 비용 비중 과도"})
        except Exception:
            pass

        # ── ★ V10.31e: 심볼별 실적 (7일 창) + 쿨다운 상태 ──
        symbol_stats_list = []
        cooldown_syms = []
        try:
            from v9.strategy.symbol_stats import compute_symbol_stats, is_symbol_cooldown
            _sstats = compute_symbol_stats()
            for _ssym, _sd in _sstats.items():
                _ss_entry = {
                    "sym": _ssym.replace("/USDT", ""),
                    "n": _sd["n"],
                    "pnl": _sd["pnl"],
                    "avg": _sd["avg"],
                    "wr": int(_sd["wr"] * 100),
                    "cooldown": is_symbol_cooldown(_ssym),
                }
                symbol_stats_list.append(_ss_entry)
                if _ss_entry["cooldown"]:
                    cooldown_syms.append(_ss_entry["sym"])
            # PnL 내림차순
            symbol_stats_list.sort(key=lambda x: x["pnl"], reverse=True)
            # 인사이트: 쿨다운 심볼 있으면 알림
            if cooldown_syms:
                insights.append({"level": "info",
                    "text": f"실적 쿨다운 중: {', '.join(cooldown_syms)} (7d PnL<0, n≥5)"})
        except Exception:
            pass

        # 샘플링 후 대시보드용 bal_history (5분 간격 최대 288포인트)
        if len(bal_history) > 288:
            bal_history = bal_history[::5]

        # ── JSON 출력 ──
        status = {
            "ts": now,
            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            "balance": round(bal, 2),
            "margin_ratio": round(mr, 4),
            "positions": sorted(positions, key=lambda x: x["roi"]),
            "summary": {
                "total_positions": len(positions),
                "long": long_count,
                "short": short_count,
                "long_notional": round(long_notional, 1),
                "short_notional": round(short_notional, 1),
                "unrealized_pnl": round(total_unrealized, 2),
            },
            "skew": skew_data,
            "today": {
                "pnl": round(today_pnl, 2),
                "trades": today_trades,
                "wins": today_wins,
                "wr": round(today_wins / today_trades * 100, 0) if today_trades > 0 else 0,
            },
            "recent_trades": recent_trades[-15:],
            "bal_history": bal_history,
            # ★ V10.31c: 신규 필드 — 오늘 탭 / 인사이트 탭용
            "daily": daily_list,        # 최근 7일 일별 PnL
            "strat_pnl": strat_pnl,     # 전략별 누적 기여도 (7일 창)
            "hour_pnl": hour_pnl,       # 오늘 시간대별 PnL (24시간)
            "insights": insights,       # 규칙 기반 경고/관찰
            # ★ V10.31d: 성과 지표
            "perf": perf,               # MDD / Sharpe / n_days / warning
            "costs_7d": {               # 7일 누수 비용
                "fee": round(fee_7d, 2),
                "funding": round(funding_7d, 2),  # 음수=지불, 양수=수취
            },
            # ★ V10.31e: 심볼별 7일 실적 + 쿨다운 상태
            "symbol_stats": symbol_stats_list,
            # ★ V10.31n: PTP 상태 — 대시보드 상단에 on/off + peak/drop 표시용
            "ptp": _build_ptp_status(system_state, bal),
        }

        tmp = _STATUS_PATH + ".tmp"
        # ★ V10.31d 핫픽스: allow_nan=False로 NaN/Inf 감지. 실패 시 perf/costs 제거 fallback
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError) as _je:
            # NaN/Inf 또는 직렬화 실패 → perf/costs 제거하고 재시도 (대시보드 전체 먹통 방지)
            print(f"[status_writer] JSON dump 실패(perf 제거 후 재시도): {_je}")
            status.pop("perf", None)
            status.pop("costs_7d", None)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(status, f, ensure_ascii=False, allow_nan=False, default=str)
        os.replace(tmp, _STATUS_PATH)

    except Exception as e:
        # ★ V10.31d 핫픽스: 대시보드 먹통 진단용 — 에러를 silent로 삼키지 않음
        import traceback
        print(f"[status_writer] write_status 예외: {e}")
        print(traceback.format_exc())
        # 봇 전체를 죽이진 않음 (return만)
