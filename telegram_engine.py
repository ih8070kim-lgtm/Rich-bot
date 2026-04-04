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
from datetime import datetime, date, timedelta

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
# 일별 PnL 히스토리 (최근 N일)
# ═════════════════════════════════════════════════════════════════
def _load_daily_pnl(days: int = 7) -> list:
    """최근 N일간 일별 PnL. Returns: [(date_str, pnl, win, total), ...]"""
    _base = os.path.dirname(os.path.abspath(__file__))
    try:
        from v9.config import LOG_DIR as _LD
        log_dir = os.path.join(_base, _LD)
    except Exception:
        log_dir = os.path.join(_base, "v9_logs")

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
    _base = os.path.dirname(os.path.abspath(__file__))
    try:
        from v9.config import LOG_DIR as _LD
        log_dir = os.path.join(_base, _LD)
    except Exception:
        log_dir = os.path.join(_base, "v9_logs")

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
    total_unrealized = 0.0

    for sym, sym_st in (st or {}).items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if p is None:
                continue
            ep = float(p.get("ep", 0.0) or 0.0)
            cp = float(prices.get(sym, ep) or ep)
            roi = calc_roi_pct(ep, cp, pos_side, LEVERAGE) if ep > 0 and cp > 0 else 0.0
            if ep > 0 and cp > 0:
                fee_deduct = (ep + cp) / ep * FEE_RATE * LEVERAGE * 100
                roi -= fee_deduct
            dca = int(p.get("dca_level", 1) or 1)
            step = int(p.get("step", 0) or 0)
            role = p.get("role", "")
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
            else:
                badge = ""

            line = f"{icon}{badge}{short_sym} T{dca}{trail} {roi:+.1f}%"
            if pos_side == "buy":
                long_lines.append(line)
            else:
                short_lines.append(line)

    long_str = " │ ".join(long_lines) if long_lines else "—"
    short_str = " │ ".join(short_lines) if short_lines else "—"
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
        ts = datetime.now().strftime('%H:%M:%S')

        if itype == IntentType.OPEN:
            _dir = "📈L" if side == "buy" else "📉S"
            _role = _meta.get("role", "")
            if _role in ("HEDGE", "CORE_HEDGE"):
                _badge = "🛡"
            elif _role == "INSURANCE_SH":
                _badge = "🩹"
            else:
                _badge = "🔁"
            await send_telegram_message(
                f"{_dir} {_badge} <b>{short_sym}</b>\n"
                f"@ {avg_px:.4f}  ${notional:.1f}  {ts}"
            )

        elif itype == IntentType.DCA:
            tier = _meta.get("tier", 2)
            _dir = "L" if side == "buy" else "S"
            await send_telegram_message(
                f"📦 <b>{short_sym}</b> DCA T{tier} {_dir}\n"
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
                roi = (raw - fee_pct) * LEVERAGE * 100
                pnl = (raw - fee_pct) * notional
            else:
                roi = pnl = 0.0

            icon = "🟢" if pnl >= 0 else "🔴"
            _labels = {
                IntentType.FORCE_CLOSE: "🚨",
                IntentType.TP1: "✅",
                IntentType.TP2: "💰",
                IntentType.TRAIL_ON: "🎯",
                IntentType.CLOSE: "🔚",
            }
            label = _labels.get(itype, "🔚")

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
        ts = datetime.now().strftime('%H:%M:%S')

        if fill_type in ("TP1_PRE", "TP1_LIMIT"):
            icon = "🟢" if pnl >= 0 else "🔴"
            await send_telegram_message(
                f"✅ <b>{short_sym}</b> TP1 Limit\n"
                f"{icon} {roi:+.1f}% <b>${pnl:+.2f}</b>  {ts}"
            )
        elif fill_type == "PENDING_OPEN":
            _dir = "📈L" if side == "buy" else "📉S"
            await send_telegram_message(
                f"{_dir} 🔁 <b>{short_sym}</b> Limit\n"
                f"@ {avg_px:.4f}  ${notional:.1f}  {ts}"
            )
        elif fill_type == "PENDING_DCA":
            await send_telegram_message(
                f"📦 <b>{short_sym}</b> DCA T{tier} Limit\n"
                f"@ {avg_px:.4f}  ${notional:.1f}  {ts}"
            )

    except Exception as e:
        print(f"[Telegram] notify_async_fill 오류: {e}")
