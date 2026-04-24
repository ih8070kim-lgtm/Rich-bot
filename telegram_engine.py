"""
V10.27e Trinity — Telegram Engine
======================================
v10.27e:
  - 실제 체결(filled_qty > 0)만 알림 (DEDUP/FAIL 노이즈 제거)
  - RESIDUAL_CLEANUP($5 미만) 알림 제거
  - report_system_status: 레짐 제거, 일별 PnL 추가, 대시보드 스타일
  - 진입/청산 알림 간결화
"""
import asyncio
import csv
import json
import os
from datetime import datetime, date, timedelta, timezone

# ★ V10.31AK: 텔레그램 표시용 KST 명시 — 로그 파일은 UTC지만 사용자 메시지는 한국 시간
KST = timezone(timedelta(hours=9))

import requests
from dotenv import load_dotenv

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api.env")
if not os.path.exists(_ENV_PATH):
    _ENV_PATH = os.path.join(os.getcwd(), "api.env")
load_dotenv(_ENV_PATH)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")


# ═════════════════════════════════════════════════════════════════
# 발송 코어
# ═════════════════════════════════════════════════════════════════
def _send_sync(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=5.0,
        )
    except Exception as e:
        print(f"[Telegram Engine] 발송 실패: {e}")


async def send_telegram_message(message: str):
    await asyncio.to_thread(_send_sync, message)


# ═════════════════════════════════════════════════════════════════
# 로그 경로 해결 (CWD 의존 버그 방지)
# ═════════════════════════════════════════════════════════════════
def _resolve_log_dir() -> str:
    """LOG_DIR 절대경로 반환."""
    _base = os.path.dirname(os.path.abspath(__file__))
    try:
        from v9.config import LOG_DIR as _LD
        return os.path.join(_base, _LD)
    except Exception:
        return os.path.join(_base, "v9_logs")


# ═════════════════════════════════════════════════════════════════
# 일별 PnL 히스토리 (최근 N일)
# ═════════════════════════════════════════════════════════════════
def _load_daily_pnl(days: int = 7) -> list:
    """최근 N일간 일별 PnL. Returns: [(date_str, pnl, win, total), ...]
    
    ★ V10.31AF: BC/CB 제외 — 코어 전략(MR/HEDGE/INSURANCE/TREND) PnL만 집계.
    BC/CB는 별도 전략이고 x1 레버리지라 코어 성과 추적에 노이즈.
    """
    log_dir = _resolve_log_dir()
    trades_path = os.path.join(log_dir, "log_trades.csv")
    if not os.path.exists(trades_path):
        return []

    daily = {}
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    try:
        with open(trades_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = row.get("time", "")
                if not ts or ts < cutoff:
                    continue
                # ★ V10.31AF: BC/CB 제외
                if str(row.get("role", "") or "") in ("BC", "CB"):
                    continue
                day_key = ts[:10]
                try:
                    pnl = float(row.get("pnl_usdt", 0.0) or 0.0)
                except (ValueError, TypeError):
                    continue
                if day_key not in daily:
                    daily[day_key] = {"pnl": 0.0, "win": 0, "total": 0}
                daily[day_key]["pnl"] += pnl
                daily[day_key]["total"] += 1
                if pnl > 0:
                    daily[day_key]["win"] += 1
    except Exception:
        return []

    result = []
    for d in sorted(daily.keys())[-days:]:
        s = daily[d]
        result.append((d, s["pnl"], s["win"], s["total"]))
    return result


def _load_trade_stats(today_str: str) -> dict:
    log_dir = _resolve_log_dir()
    trades_path = os.path.join(log_dir, "log_trades.csv")
    stats = {
        "total": 0, "win": 0, "loss": 0, "pnl": 0.0,
        "tier": {},
        "entry": {
            "MR": {"total": 0, "win": 0, "pnl": 0.0},
            "HEDGE": {"total": 0, "win": 0, "pnl": 0.0},
            "INSURANCE": {"total": 0, "win": 0, "pnl": 0.0},
        },
    }
    if not os.path.exists(trades_path):
        return stats
    try:
        with open(trades_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if not row.get("time", "").startswith(today_str):
                    continue
                # ★ V10.31AF: BC/CB 제외 — 코어 전략 성과 분리
                if str(row.get("role", "") or "") in ("BC", "CB"):
                    continue
                try:
                    pnl = float(row.get("pnl_usdt", 0.0) or 0.0)
                    tier = max(1, min(4, int(row.get("dca_level", 1) or 1)))
                except (ValueError, TypeError):
                    continue
                is_win = pnl > 0
                stats["total"] += 1
                stats["pnl"] += pnl
                if is_win:
                    stats["win"] += 1
                else:
                    stats["loss"] += 1
                tk = f"T{tier}"
                if tk not in stats["tier"]:
                    stats["tier"][tk] = {"total": 0, "win": 0, "pnl": 0.0}
                stats["tier"][tk]["total"] += 1
                stats["tier"][tk]["pnl"] += pnl
                if is_win:
                    stats["tier"][tk]["win"] += 1
                _role = str(row.get("role", "") or "")
                if "HEDGE" in _role:
                    etype = "HEDGE"
                elif "INSURANCE" in _role:
                    etype = "INSURANCE"
                else:
                    etype = "MR"
                if etype not in stats["entry"]:
                    etype = "MR"
                stats["entry"][etype]["total"] += 1
                stats["entry"][etype]["pnl"] += pnl
                if is_win:
                    stats["entry"][etype]["win"] += 1
    except Exception:
        pass
    return stats


# ═════════════════════════════════════════════════════════════════
# 정기 리포트  (v10.27e: 대시보드 스타일)
# ═════════════════════════════════════════════════════════════════

async def report_system_status(snapshot, st: dict):
    """V10.27e 대시보드 리포트."""
    from v9.config import LEVERAGE, FEE_RATE, VERSION
    from v9.utils.utils_math import calc_roi_pct
    from v9.execution.position_book import iter_positions

    today_str_val = date.today().isoformat()
    s = _load_trade_stats(today_str_val)

    current_bal = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    base_bal = float(getattr(snapshot, "baseline_balance", 0.0) or 0.0)
    daily_roi = (current_bal - base_bal) / base_bal * 100 if base_bal > 0 else 0.0
    daily_icon = "🟢" if daily_roi >= 0 else "🔴"

    prices = getattr(snapshot, "all_prices", {}) or {}
    long_lines = []
    short_lines = []
    sub_lines   = []  # ★ V10.31c: BC/CB 별도 섹션
    total_unrealized = 0.0

    for sym, sym_st in (st or {}).items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if p is None:
                continue
            ep = float(p.get("ep", 0.0) or 0.0)
            cp = float(prices.get(sym, ep) or ep)
            role = p.get("role", "")
            # ★ V10.29e: BC/CB는 x1 레버리지
            _dash_lev = 1 if role in ("BC", "CB") else LEVERAGE
            roi = calc_roi_pct(ep, cp, pos_side, _dash_lev) if ep > 0 and cp > 0 else 0.0
            if ep > 0 and cp > 0:
                fee_deduct = (ep + cp) / ep * FEE_RATE * _dash_lev * 100
                roi -= fee_deduct
            dca = int(p.get("dca_level", 1) or 1)
            step = int(p.get("step", 0) or 0)
            amt = float(p.get("amt", 0.0) or 0.0)

            if ep > 0 and cp > 0:
                if pos_side == "buy":
                    total_unrealized += amt * (cp - ep)
                else:
                    total_unrealized += amt * (ep - cp)

            icon = "🟢" if roi >= 0 else "🔴"
            trail = "✂" if step >= 1 else ""
            short_sym = sym.replace("/USDT", "")

            if role in ("HEDGE", "SOFT_HEDGE", "CORE_HEDGE"):
                badge = "🛡"
            elif role == "INSURANCE_SH":
                badge = "🩹"
            elif role == "BC":
                badge = "🌀"
            elif role == "CB":
                badge = "⚡"
            else:
                badge = ""

            line = f"{icon}{badge}{short_sym} T{dca}{trail} {roi:+.1f}%"
            # ★ V10.31c: BC/CB는 별도 섹션으로 분리 (코어 전략과 구분)
            if role in ("BC", "CB"):
                sub_lines.append(line)
            elif pos_side == "buy":
                long_lines.append(line)
            else:
                short_lines.append(line)

    long_str = " │ ".join(long_lines) if long_lines else "—"
    short_str = " │ ".join(short_lines) if short_lines else "—"
    sub_str   = " │ ".join(sub_lines)   if sub_lines   else ""
    upnl_icon = "🟢" if total_unrealized >= 0 else "🔴"

    pnl_icon = "🟢" if s["pnl"] >= 0 else "🔴"
    wr = f'{s["win"]}/{s["total"]}' if s["total"] else "—"

    daily_history = _load_daily_pnl(7)
    hist_lines = []
    for dstr, dpnl, dwin, dtotal in daily_history[-5:]:
        bar = "▓" if dpnl >= 0 else "░"
        dwr = f"{dwin}/{dtotal}" if dtotal else "—"
        hist_lines.append(f"{dstr[5:]} {bar} ${dpnl:+.1f} ({dwr})")
    hist_str = "\n".join(hist_lines) if hist_lines else "기록 없음"

    _real_bal = current_bal or 1.0
    _long_m = sum(
        float(p_.get("amt", 0.0)) * float(p_.get("ep", 0.0))
        for ss in (st or {}).values() if isinstance(ss, dict)
        for sd, p_ in iter_positions(ss) if sd == "buy" and isinstance(p_, dict)
    ) / LEVERAGE / _real_bal * 100
    _short_m = sum(
        float(p_.get("amt", 0.0)) * float(p_.get("ep", 0.0))
        for ss in (st or {}).values() if isinstance(ss, dict)
        for sd, p_ in iter_positions(ss) if sd == "sell" and isinstance(p_, dict)
    ) / LEVERAGE / _real_bal * 100

    # ★ V10.31c: BC/CB는 별도 섹션, 코어(MR/TREND)와 명확히 분리
    _sub_section = f"🌀 보조 {sub_str}\n━━━━━━━━━━━━━━━━\n" if sub_str else ""

    msg = (
        f"<b>Trinity v{VERSION}</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 <b>${current_bal:,.2f}</b>  {daily_icon} {daily_roi:+.2f}%\n"
        f"{upnl_icon} 미실현 <b>${total_unrealized:+.2f}</b>\n"
        f"📊 L {_long_m:.0f}% │ S {_short_m:.0f}%\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📈 {long_str}\n"
        f"📉 {short_str}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{_sub_section}"
        f"{pnl_icon} 금일 <b>${s['pnl']:+.2f}</b> ({wr})\n"
        f"<pre>{hist_str}</pre>"
    )
    await send_telegram_message(msg)


# ═════════════════════════════════════════════════════════════════
# 체결 알림  (v10.27e: 실제 체결만, 간결 포맷)
# ═════════════════════════════════════════════════════════════════
async def notify_fill(result, intent, st: dict = None, snapshot=None, pos_snap: dict = None):
    try:
        from v9.types import IntentType
        from v9.config import LEVERAGE, FEE_RATE

        filled = float(result.filled_qty or 0.0)
        if filled <= 0:
            return

        sym = result.symbol
        itype = intent.intent_type
        side = result.side
        avg_px = float(result.avg_price or 0.0)
        notional = avg_px * filled
        _meta = intent.metadata or {}

        # RESIDUAL_CLEANUP 무음
        if itype == IntentType.FORCE_CLOSE and "RESIDUAL" in (intent.reason or ""):
            return

        short_sym = sym.replace("/USDT", "")
        ts = datetime.now(KST).strftime('%H:%M:%S')  # ★ V10.31AK: KST 명시

        if itype == IntentType.OPEN:
            _dir = "📈L" if side == "buy" else "📉S"
            _role = _meta.get("role", "")
            _entry = _meta.get("entry_type", "")
            if _role in ("HEDGE", "CORE_HEDGE"):
                _badge = "🛡"
            elif _role == "INSURANCE_SH":
                _badge = "🩹"
            elif _role == "BC":
                _badge = "🌀"
            elif _entry == "TREND":
                _badge = "🔀"
            elif _entry == "COUNTER":
                _badge = "⚡"
            else:
                _badge = "🔁"
            await send_telegram_message(
                f"{_dir} {_badge} <b>{short_sym}</b>\n"
                f"@ {avg_px:.4f}  ${notional:.1f}  {ts}"
            )

        elif itype == IntentType.DCA:
            tier = _meta.get("tier", 2)
            _dir = "L" if side == "buy" else "S"
            _is_urgency = "URGENCY" in (intent.reason or "")
            _dca_badge = "⚠️" if _is_urgency else "📦"
            _dca_label = "URG" if _is_urgency else "DCA"
            await send_telegram_message(
                f"{_dca_badge} <b>{short_sym}</b> {_dca_label} T{tier} {_dir}\n"
                f"@ {avg_px:.4f}  ${notional:.1f}  {ts}"
            )

        elif itype in (IntentType.TP1, IntentType.TP2, IntentType.TRAIL_ON,
                       IntentType.CLOSE, IntentType.FORCE_CLOSE):
            _p = pos_snap or {}
            ep = float(_p.get("ep", 0.0) or 0.0)
            dca = int(_p.get("dca_level", 1) or 1)

            if ep > 0 and avg_px > 0:
                raw = (avg_px - ep) / ep if side == "sell" else (ep - avg_px) / ep
                fee_pct = (ep + avg_px) / ep * FEE_RATE
                # ★ V10.29e: BC/CB는 x1 레버리지 — ROI 정확 표시
                _exit_role = (_p or {}).get("role", "")
                _exit_lev = 1 if _exit_role in ("BC", "CB") else LEVERAGE
                roi = (raw - fee_pct) * _exit_lev * 100
                pnl = (raw - fee_pct) * notional
            else:
                roi = pnl = 0.0

            icon = "🟢" if pnl >= 0 else "🔴"
            _reason = intent.reason or ""
            # ★ V10.29d: 청산 유형별 아이콘
            if "TRIM" in _reason:
                label = "✂️"
            elif itype == IntentType.FORCE_CLOSE:
                if "ZOMBIE" in _reason:
                    label = "💀"
                else:
                    label = "🚨"
            elif itype == IntentType.TRAIL_ON:
                label = "🎯"
            elif itype == IntentType.TP1:
                label = "✅"
            elif itype == IntentType.TP2:
                label = "💰"
            else:
                label = "🔚"

            await send_telegram_message(
                f"{label} <b>{short_sym}</b> T{dca}\n"
                f"{icon} {roi:+.1f}% <b>${pnl:+.2f}</b>  {ts}"
            )

    except Exception as e:
        print(f"[Telegram] notify_fill 오류: {e}")


# ═════════════════════════════════════════════════════════════════
# 비동기 체결 알림 (Pending Limit)
# ═════════════════════════════════════════════════════════════════
async def notify_async_fill(
    sym: str, side: str, avg_px: float, filled: float,
    fill_type: str, pnl: float = 0.0, roi: float = 0.0,
    ep: float = 0.0, tier: int = 0, role: str = "",
):
    try:
        if filled <= 0:
            return

        notional = avg_px * filled
        short_sym = sym.replace("/USDT", "")
        ts = datetime.now(KST).strftime('%H:%M:%S')  # ★ V10.31AK: KST 명시

        if fill_type in ("TP1_PRE", "TP1_LIMIT"):
            icon = "🟢" if pnl >= 0 else "🔴"
            await send_telegram_message(
                f"✅ <b>{short_sym}</b> TP1 Limit\n"
                f"{icon} {roi:+.1f}% <b>${pnl:+.2f}</b>  {ts}"
            )
        elif fill_type == "TRIM_FILL":
            icon = "🟢" if pnl >= 0 else "🔴"
            await send_telegram_message(
                f"✂️ <b>{short_sym}</b> Trim T{tier}\n"
                f"{icon} {roi:+.1f}% <b>${pnl:+.2f}</b>  {ts}"
            )
        elif fill_type == "PENDING_OPEN":
            _dir = "📈L" if side == "buy" else "📉S"
            _badge = "🌀" if role == "BC" else "🔁"
            await send_telegram_message(
                f"{_dir} {_badge} <b>{short_sym}</b> Limit\n"
                f"@ {avg_px:.4f}  ${notional:.1f}  {ts}"
            )
        elif fill_type == "PENDING_DCA":
            await send_telegram_message(
                f"📦 <b>{short_sym}</b> DCA T{tier} Limit\n"
                f"@ {avg_px:.4f}  ${notional:.1f}  {ts}"
            )

    except Exception as e:
        print(f"[Telegram] notify_async_fill 오류: {e}")


# ═════════════════════════════════════════════════════════════════
# ★ V10.28b: 일일 리포트 생성 (재사용 가능)
# ═════════════════════════════════════════════════════════════════
def _load_trades_for_date(target_date: str) -> list:
    """특정 날짜의 log_trades.csv 행을 파싱하여 dict list 반환."""
    log_dir = _resolve_log_dir()
    trades_path = os.path.join(log_dir, "log_trades.csv")
    if not os.path.exists(trades_path):
        return []

    trades = []
    try:
        with open(trades_path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ts = row.get("time", "")
                if not ts.startswith(target_date):
                    continue
                try:
                    pnl = float(row.get("pnl_usdt", 0) or 0)
                    roi = float(row.get("roi_pct", 0) or 0)
                    dca = max(1, min(4, int(row.get("dca_level", 1) or 1)))
                    hold = float(row.get("hold_sec", 0) or 0)
                except (ValueError, TypeError):
                    continue
                trades.append({
                    "sym":    row.get("symbol", "").replace("/USDT", ""),
                    "side":   row.get("side", ""),
                    "pnl":    pnl,
                    "roi":    roi,
                    "dca":    dca,
                    "reason": row.get("reason", ""),
                    "hold":   hold,
                    "entry":  row.get("entry_type", ""),
                    "role":   row.get("role", ""),
                })
    except Exception as e:
        print(f"[Report] trades 로드 실패: {e}")
    return trades


def generate_daily_report(target_date: str = None, active_positions: int = None) -> str:
    """
    일별 트레이드 리포트 문자열 생성.
    target_date: "YYYY-MM-DD" (None이면 어제)
    active_positions: 현재 활성 포지션 수 (runner에서 전달, bot에서는 state에서 읽음)
    """
    if target_date is None:
        target_date = (date.today() - timedelta(days=1)).isoformat()

    trades = _load_trades_for_date(target_date)

    if not trades:
        return (
            f"📊 <b>일일 리포트</b> ({target_date})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"트레이드: 0건"
        )

    # ★ V10.31AF: 코어 전략(MR/HEDGE/INSURANCE/TREND)과 BC/CB 분리
    # 메인 집계(PnL, WR, avg, best/worst, Tier, Reason, Trim, hold)는 core_trades만 사용.
    # role_map(🎭 섹션)에서만 BC/CB 별도 표시로 참고 가능.
    core_trades = [t for t in trades if t.get("role", "") not in ("BC", "CB")]
    sub_trades  = [t for t in trades if t.get("role", "") in ("BC", "CB")]
    
    if not core_trades:
        # 코어 거래 없고 BC/CB만 있는 경우 — 참고용 간단 표시
        sub_pnl = sum(t["pnl"] for t in sub_trades)
        sub_n   = len(sub_trades)
        return (
            f"📊 <b>일일 리포트</b> ({target_date})\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"코어 트레이드: 0건\n"
            f"(참고) BC/CB: {sub_n}건 ${sub_pnl:+.2f}"
        )

    n = len(core_trades)
    wins = sum(1 for t in core_trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in core_trades)
    avg_pnl = total_pnl / n
    wr = wins / n * 100

    best = max(core_trades, key=lambda t: t["pnl"])
    worst = min(core_trades, key=lambda t: t["pnl"])
    pnl_icon = "🟢" if total_pnl >= 0 else "🔴"

    # ── Tier 분포 (코어만) ──
    tier_map = {}
    for t in core_trades:
        tk = t["dca"]
        if tk not in tier_map:
            tier_map[tk] = {"n": 0, "w": 0, "pnl": 0.0}
        tier_map[tk]["n"] += 1
        tier_map[tk]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            tier_map[tk]["w"] += 1

    tier_lines = []
    for tk in sorted(tier_map):
        d = tier_map[tk]
        twr = d["w"] / d["n"] * 100 if d["n"] else 0
        tier_lines.append(f"  T{tk}: {d['n']}건 ${d['pnl']:+.1f} ({twr:.0f}%)")
    tier_str = "\n".join(tier_lines)

    # ── 청산 유형 (코어만) ──
    reason_counts = {}
    trim_pnl = 0.0
    trim_count = 0
    for t in core_trades:
        r = t["reason"]
        if "TRAIL" in r:
            key = "Trail"
        elif "TP1" in r:
            key = "TP1"
        elif "TRIM" in r:
            key = "Trim"
            trim_pnl += t["pnl"]
            trim_count += 1
        elif "HARD_SL" in r:
            key = "SL"
        elif "FORCE" in r or "PAIR_CUT" in r:
            key = "FC"
        elif "CLOSE" in r or "CORR" in r:
            key = "Close"
        elif "GHOST" in r:
            key = "Ghost"
        else:
            key = r[:8] if r else "기타"
        reason_counts[key] = reason_counts.get(key, 0) + 1

    exit_str = " / ".join(f"{k} {v}" for k, v in sorted(reason_counts.items(), key=lambda x: -x[1]))

    # ── Role/전략 분포 (원본 trades 사용 — BC/CB 별도 라벨 표시) ──
    role_map = {}
    for t in trades:
        rl = t["role"]
        et = t.get("entry", "")
        if rl == "BC":
            rk = "BC"
        elif rl == "CB":
            rk = "CB"
        elif "HEDGE" in rl:
            rk = "Hedge"
        elif "INSURANCE" in rl:
            rk = "Ins"
        elif et == "TREND":
            rk = "Trend"
        elif "BREAKOUT" in rl or "E30" in rl:
            rk = "E30"
        else:
            rk = "MR"
        if rk not in role_map:
            role_map[rk] = {"n": 0, "pnl": 0.0}
        role_map[rk]["n"] += 1
        role_map[rk]["pnl"] += t["pnl"]

    role_str = " / ".join(f"{k} {d['n']}건 ${d['pnl']:+.1f}" for k, d in sorted(role_map.items(), key=lambda x: -x[1]["n"]))

    # ── 평균 보유시간 (코어만) ──
    valid_holds = [t["hold"] for t in core_trades if t["hold"] > 0]
    if valid_holds:
        avg_hold_sec = sum(valid_holds) / len(valid_holds)
        if avg_hold_sec >= 3600:
            hold_str = f"{avg_hold_sec / 3600:.1f}h"
        else:
            hold_str = f"{avg_hold_sec / 60:.0f}m"
    else:
        hold_str = "—"

    # ── 7일 히스토리 ──
    daily_hist = _load_daily_pnl(7)
    hist_lines = []
    cumulative = 0.0
    for dstr, dpnl, dwin, dtotal in daily_hist:
        cumulative += dpnl
        bar = "▓" if dpnl >= 0 else "░"
        dwr = f"{dwin}/{dtotal}" if dtotal else "—"
        hist_lines.append(f"{dstr[5:]} {bar} ${dpnl:+.1f} ({dwr})")
    hist_str = "\n".join(hist_lines) if hist_lines else "기록 없음"
    cum_icon = "🟢" if cumulative >= 0 else "🔴"

    # ── 조립 ──
    pos_line = ""
    if active_positions is not None:
        pos_line = f"\n📌 현재 포지션: {active_positions}개"

    msg = (
        f"📊 <b>일일 리포트</b> ({target_date})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{pnl_icon} PnL <b>${total_pnl:+.2f}</b>  {wins}/{n} ({wr:.0f}%)\n"
        f"평균 ${avg_pnl:+.2f} / 보유 {hold_str}\n"
        f"📈 Best: {best['sym']} ${best['pnl']:+.2f}\n"
        f"📉 Worst: {worst['sym']} ${worst['pnl']:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 <b>Tier</b>\n"
        f"{tier_str}\n"
        f"🔚 {exit_str}\n"
        f"✂️ Trim: {trim_count}건 ${trim_pnl:+.2f}\n"
        f"🎭 {role_str}"
        f"{pos_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📅 <b>7일 히스토리</b>\n"
        f"<pre>{hist_str}</pre>\n"
        f"{cum_icon} 7일합계 <b>${cumulative:+.2f}</b>"
    )
    return msg


async def send_daily_report(target_date: str = None, active_positions: int = None):
    """일일 리포트 생성 후 텔레그램 발송."""
    msg = generate_daily_report(target_date, active_positions)
    await send_telegram_message(msg)
