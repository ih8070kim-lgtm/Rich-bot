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
            # 대시보드용으로 5분 간격 샘플링 (최대 288포인트)
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
        }

        tmp = _STATUS_PATH + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(status, f, ensure_ascii=False)
        os.replace(tmp, _STATUS_PATH)

    except Exception as e:
        # 대시보드 오류가 봇 전체를 죽이면 안 됨
        pass
