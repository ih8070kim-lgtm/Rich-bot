"""
V10.7 Trinity — Telegram Engine
======================================
v10.7:
  - 롱/숏 마진율 분리 표시
  - SOFT_HEDGE 배지 (🔰) 추가
  - Pullback 배지 제거 → Breakout 배지 (🚀) 추가
  - 버전 표기 V10.7 갱신
"""
import asyncio
import csv
import json
import os
from datetime import datetime, date

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
        print(f"🚨 [Telegram Engine] 발송 실패: {e}")


async def send_telegram_message(message: str):
    await asyncio.to_thread(_send_sync, message)


# ═════════════════════════════════════════════════════════════════
# 정기 리포트  (v10.6: hedge mode, regime, MR/PB 구분)
# ═════════════════════════════════════════════════════════════════

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
        "tier":  {1:{"total":0,"win":0}, 2:{"total":0,"win":0}, 3:{"total":0,"win":0}},
        "entry": {
            "MR":{"total":0,"win":0}, "HEDGE":{"total":0,"win":0},
            "BALANCE":{"total":0,"win":0}, "INSURANCE":{"total":0,"win":0},
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
                    pnl  = float(row.get("pnl_usdt", 0.0) or 0.0)
                    tier = max(1, min(3, int(row.get("dca_level", 1) or 1)))
                except (ValueError, TypeError):
                    continue
                is_win = pnl > 0
                stats["total"] += 1
                stats["pnl"]   += pnl
                if is_win: stats["win"]  += 1
                else:      stats["loss"] += 1
                stats["tier"][tier]["total"] += 1
                if is_win: stats["tier"][tier]["win"] += 1
                etype = str(row.get("entry_type", "MR") or "MR")
                _role = str(row.get("role", "") or "")
                # role 기반 분류 (v10.11b)
                if "HEDGE" in _role: etype = "HEDGE"
                elif "BALANCE" in _role: etype = "BALANCE"
                elif "INSURANCE" in _role: etype = "INSURANCE"
                else: etype = "MR"
                if etype not in stats["entry"]: etype = "MR"
                stats["entry"][etype]["total"] += 1
                if is_win: stats["entry"][etype]["win"] += 1
    except Exception:
        pass
    return stats


def _wr(d: dict) -> str:
    t = d.get("total", 0)
    w = d.get("win", 0)
    if t == 0: return "N/A"
    return f"{w/t*100:.0f}% ({w}/{t})"


async def report_system_status(snapshot, st: dict):
    """V10.6 정기 보고."""
    from v9.config import LEVERAGE, FEE_RATE
    from v9.utils.utils_math import calc_roi_pct
    from v9.execution.position_book import iter_positions

    today_str_val = date.today().isoformat()
    s = _load_trade_stats(today_str_val)

    total = s["total"]
    wr_total = _wr({"total": total, "win": s["win"]})
    pnl_icon = "🟢" if s["pnl"] >= 0 else "🔴"

    # 레짐
    try:
        from v9.strategy.planners import _btc_vol_regime
        regime = _btc_vol_regime(snapshot)
    except Exception:
        regime = "?"
    regime_map = {"HIGH": "⚡ HIGH", "NORMAL": "🟢 NORMAL", "LOW": "😴 LOW", "BAD": "💀 BAD"}
    regime_ui = regime_map.get(regime, regime)

    # 포지션 목록 (v10.6: iter_positions 사용)
    prices    = getattr(snapshot, "all_prices", {}) or {}
    long_pos  = []
    short_pos = []
    hedge_pos = []

    for sym, sym_st in (st or {}).items():
        if not isinstance(sym_st, dict):
            continue
        for pos_side, p in iter_positions(sym_st):
            if p is None:
                continue
            ep   = float(p.get("ep", 0.0) or 0.0)
            cp   = float(prices.get(sym, ep) or ep)
            roi  = calc_roi_pct(ep, cp, pos_side, LEVERAGE) if ep > 0 and cp > 0 else 0.0
            # ★ v10.9: 왕복 수수료 차감 (net ROI)
            if ep > 0 and cp > 0:
                fee_deduct = (ep + cp) / ep * FEE_RATE * LEVERAGE * 100
                roi -= fee_deduct
            dca  = int(p.get("dca_level", 1) or 1)
            step = int(p.get("step", 0) or 0)
            role = p.get("role", "")
            entry_type = p.get("entry_type", "MR")

            roi_icon = "🟢" if roi >= 0 else "🔴"
            trail_tag = "✂️" if step >= 1 else ""

            if role in ("HEDGE", "SOFT_HEDGE"):
                direction = "L" if pos_side == "buy" else "S"
                _h_badge = "🛡️" if role == "HEDGE" else "🔰"
                entry = f"{roi_icon}{_h_badge} {sym} [{direction}]: {roi:+.2f}%"
                hedge_pos.append(entry)
            elif role == "CORE_HEDGE":
                entry = f"{roi_icon}🛡️ {sym} T{dca}{trail_tag}: {roi:+.2f}%"
                hedge_pos.append(entry)
            elif role == "INSURANCE_SH":
                entry = f"{roi_icon}🩹 {sym} INS: {roi:+.2f}%"
                (long_pos if pos_side == "buy" else short_pos).append(entry)
            elif role == "CORE_BALANCE":
                entry = f"{roi_icon}⚖️ {sym} T{dca}{trail_tag}: {roi:+.2f}%"
                (long_pos if pos_side == "buy" else short_pos).append(entry)
            else:
                type_badge = "🚀" if entry_type == "BREAKOUT" else "🔁"
                entry = f"{roi_icon}{type_badge} {sym} T{dca}{trail_tag}: {roi:+.2f}%"
                (long_pos if pos_side == "buy" else short_pos).append(entry)

    long_status  = "\n  └ " + "\n  └ ".join(long_pos)  if long_pos  else "  └ 없음"
    short_status = "\n  └ " + "\n  └ ".join(short_pos) if short_pos else "  └ 없음"
    hedge_status = "\n🛡️ <b>Hedge</b>\n  └ " + "\n  └ ".join(hedge_pos) if hedge_pos else ""

    current_bal = float(getattr(snapshot, "real_balance_usdt", 0.0) or 0.0)
    base_bal    = float(getattr(snapshot, "baseline_balance",  0.0) or 0.0)
    mdd_pct     = (current_bal - base_bal) / base_bal * 100 if base_bal > 0 else 0.0

    # ★ v10.7: 롱/숏 마진율 분리 계산
    _real_bal_tg = current_bal or 1.0
    _long_margin_tg = sum(
        float(p_.get("amt", 0.0)) * float(p_.get("ep", 0.0))
        for sym_st_ in (st or {}).values()
        if isinstance(sym_st_, dict)
        for side_, p_ in iter_positions(sym_st_)
        if side_ == "buy" and isinstance(p_, dict)
    ) / LEVERAGE / _real_bal_tg * 100
    _short_margin_tg = sum(
        float(p_.get("amt", 0.0)) * float(p_.get("ep", 0.0))
        for sym_st_ in (st or {}).values()
        if isinstance(sym_st_, dict)
        for side_, p_ in iter_positions(sym_st_)
        if side_ == "sell" and isinstance(p_, dict)
    ) / LEVERAGE / _real_bal_tg * 100
    _total_margin_tg = _long_margin_tg + _short_margin_tg
    _margin_bar = f"L {_long_margin_tg:.0f}% | S {_short_margin_tg:.0f}% | T {_total_margin_tg:.0f}%"

    mr_s = s["entry"]["MR"]
    hg_s = s["entry"]["HEDGE"]
    bl_s = s["entry"]["BALANCE"]
    in_s = s["entry"]["INSURANCE"]

    # 승률 표시 (건수 있는 것만)
    _stats_lines = [f"  🔁MR: {_wr(mr_s)}"]
    if hg_s["total"] > 0: _stats_lines.append(f"  🛡️헷지: {_wr(hg_s)}")
    if bl_s["total"] > 0: _stats_lines.append(f"  ⚖️밸런스: {_wr(bl_s)}")
    if in_s["total"] > 0: _stats_lines.append(f"  🩹보험: {_wr(in_s)}")
    _stats_str = "\n".join(_stats_lines)

    msg = (
        f"<b>Trinity V10.11b 리포트</b>\n"
        f"────────────────\n"
        f"🌡️ <b>{regime_ui}</b>  💰 <b>${current_bal:,.2f}</b>  일MDD: {mdd_pct:+.2f}%\n"
        f"📊 마진: {_margin_bar}\n"
        f"────────────────\n"
        f"📈 <b>Long</b>{long_status}\n\n"
        f"📉 <b>Short</b>{short_status}"
        f"{hedge_status}\n"
        f"────────────────\n"
        f"📋 <b>금일 {total}건</b> 승률: {wr_total}  {pnl_icon} ${s['pnl']:+.2f}\n"
        f"{_stats_str}\n"
        f"────────────────\n"
    )
    await send_telegram_message(msg)


# ═════════════════════════════════════════════════════════════════
# 체결 알림  (v10.6: DCA 추가, 헷지 배지 role 기반)
# ═════════════════════════════════════════════════════════════════
async def notify_fill(result, intent, st: dict = None, snapshot=None, pos_snap: dict = None):
    try:
        from v9.types import IntentType
        from v9.config import LEVERAGE, FEE_RATE

        sym      = result.symbol
        itype    = intent.intent_type
        side     = result.side
        avg_px   = float(result.avg_price  or 0.0)
        filled   = float(result.filled_qty or 0.0)
        notional = avg_px * filled
        _meta    = intent.metadata or {}

        # ── 진입 알림 ──────────────────────────────────────────
        if itype == IntentType.OPEN:
            side_emoji = "📈 <b>롱 진입</b>" if side == "buy" else "📉 <b>숏 진입</b>"
            _role = _meta.get("role", "")
            _etype = _meta.get("entry_type", "MR")
            if _role in ("HEDGE", "CORE_HEDGE"):
                etype_badge = "🛡️ <b>헷지</b>"
            elif _role == "INSURANCE_SH":
                etype_badge = "🩹 <b>보험</b>"
            elif _role == "CORE_BALANCE":
                etype_badge = "⚖️ <b>밸런스</b>"
            elif _etype == "PULLBACK":
                etype_badge = "📐 <b>Pullback</b>"
            else:
                etype_badge = "🔁 <b>MR</b>"
            await send_telegram_message(
                f"{side_emoji}  {etype_badge}\n"
                f"────────────────\n"
                f"📌 <b>{sym}</b>\n"
                f"💵 진입가: <b>{avg_px:.4f}</b>\n"
                f"📦 수량: {filled:.4f} | 💰 ${notional:.2f}\n"
                f"⏱ {datetime.now().strftime('%H:%M:%S')}"
            )

        # ── DCA 알림 (v10.6 신규) ──────────────────────────────
        elif itype == IntentType.DCA:
            tier = _meta.get("tier", 2)
            _role = _meta.get("role", "")
            badge = "🛡️" if _role in ("HEDGE", "CORE_HEDGE") else ("⚖️" if _role == "CORE_BALANCE" else "📦")
            await send_telegram_message(
                f"{badge} <b>DCA T{tier}</b>\n"
                f"────────────────\n"
                f"📌 <b>{sym}</b>  {'롱' if side == 'buy' else '숏'}\n"
                f"💵 체결가: <b>{avg_px:.4f}</b>\n"
                f"📦 추가: {filled:.4f} | 💰 ${notional:.2f}\n"
                f"⏱ {datetime.now().strftime('%H:%M:%S')}"
            )

        # ── TP2 추가 익절 ──────────────────────────────────────
        elif itype == IntentType.TP2:
            _p = pos_snap or {}
            ep = float(_p.get("ep", 0.0) or 0.0)
            if ep > 0 and avg_px > 0:
                raw = (avg_px - ep) / ep if side == "sell" else (ep - avg_px) / ep
                fee_pct = (ep + avg_px) / ep * FEE_RATE  # 왕복 수수료
                roi = (raw - fee_pct) * LEVERAGE * 100
                pnl = (raw - fee_pct) * notional
            else:
                roi = pnl = 0.0
            emoji = "🟢" if roi >= 0 else "🔴"
            await send_telegram_message(
                f"💰 <b>TP2 추가 익절</b>\n"
                f"────────────────\n"
                f"📌 <b>{sym}</b>\n"
                f"💵 청산가: <b>{avg_px:.4f}</b>  (진입: {ep:.4f})\n"
                f"{emoji} <b>{roi:+.2f}%</b>  💵 <b>${pnl:+.2f}</b>\n"
                f"⏱ {datetime.now().strftime('%H:%M:%S')}"
            )

        # ── 청산 알림 ──────────────────────────────────────────
        elif itype in (IntentType.TP1, IntentType.TRAIL_ON,
                       IntentType.CLOSE, IntentType.FORCE_CLOSE):
            _p = pos_snap or {}
            ep = float(_p.get("ep", 0.0) or 0.0)
            role = _p.get("role", "")

            if ep > 0 and avg_px > 0:
                raw = (avg_px - ep) / ep if side == "sell" else (ep - avg_px) / ep
                fee_pct = (ep + avg_px) / ep * FEE_RATE  # 왕복 수수료
                roi = (raw - fee_pct) * LEVERAGE * 100
                pnl = (raw - fee_pct) * notional
            else:
                roi = pnl = 0.0

            badge = "🛡️ " if role in ("HEDGE", "CORE_HEDGE") else ("🩹 " if role == "INSURANCE_SH" else ("⚖️ " if role == "CORE_BALANCE" else ""))
            labels = {
                IntentType.FORCE_CLOSE: f"🚨 <b>{badge}강제청산</b>",
                IntentType.TP1:         f"✅ <b>{badge}익절 (TP1)</b>",
                IntentType.TRAIL_ON:    f"🎯 <b>{badge}트레일링 청산</b>",
                IntentType.CLOSE:       f"🔚 <b>{badge}청산</b>",
            }
            type_label = labels.get(itype, f"🔚 <b>{badge}청산</b>")
            emoji = "🟢" if roi >= 0 else "🔴"

            await send_telegram_message(
                f"{type_label}\n"
                f"────────────────\n"
                f"📌 <b>{sym}</b>\n"
                f"💵 청산가: <b>{avg_px:.4f}</b>  (진입: {ep:.4f})\n"
                f"{emoji} <b>{roi:+.2f}%</b>  💵 <b>${pnl:+.2f}</b>\n"
                f"⏱ {datetime.now().strftime('%H:%M:%S')}"
            )

    except Exception as e:
        print(f"[Telegram] notify_fill 오류: {e}")


# ═════════════════════════════════════════════════════════════════
# ★ V10.17: 비동기 체결 알림 (TP1 선주문 / Pending Limit)
# notify_fill은 Intent+OrderResult 객체 필요 → 비동기 매니저는 이 객체 없음
# ═════════════════════════════════════════════════════════════════
async def notify_async_fill(
    sym: str, side: str, avg_px: float, filled: float,
    fill_type: str, pnl: float = 0.0, roi: float = 0.0,
    ep: float = 0.0, tier: int = 0, role: str = "",
):
    """TP1 선주문 / Pending Limit 체결 알림 (경량 버전)."""
    try:
        notional = avg_px * filled
        badge = "🛡️ " if "HEDGE" in role else ("🩹 " if "INSURANCE" in role else "")
        ts_str = datetime.now().strftime('%H:%M:%S')

        if fill_type == "TP1_PRE":
            emoji = "🟢" if pnl >= 0 else "🔴"
            msg = (
                f"✅ <b>{badge}TP1 선주문 체결</b>\n"
                f"────────────────\n"
                f"📌 <b>{sym}</b>\n"
                f"💵 청산가: <b>{avg_px:.4f}</b>  (진입: {ep:.4f})\n"
                f"{emoji} <b>{roi:+.2f}%</b>  💵 <b>${pnl:+.2f}</b>\n"
                f"📦 {filled:.4f} | 💰 ${notional:.2f}\n"
                f"⏱ {ts_str}"
            )
        elif fill_type == "PENDING_OPEN":
            side_emoji = "📈 <b>롱 진입</b>" if side == "buy" else "📉 <b>숏 진입</b>"
            msg = (
                f"{side_emoji}  🔁 <b>{badge}Limit 체결</b>\n"
                f"────────────────\n"
                f"📌 <b>{sym}</b>\n"
                f"💵 체결가: <b>{avg_px:.4f}</b>\n"
                f"📦 {filled:.4f} | 💰 ${notional:.2f}\n"
                f"⏱ {ts_str}"
            )
        elif fill_type == "PENDING_DCA":
            msg = (
                f"📦 <b>{badge}DCA T{tier} Limit 체결</b>\n"
                f"────────────────\n"
                f"📌 <b>{sym}</b>  {'롱' if side == 'buy' else '숏'}\n"
                f"💵 체결가: <b>{avg_px:.4f}</b>\n"
                f"📦 {filled:.4f} | 💰 ${notional:.2f}\n"
                f"⏱ {ts_str}"
            )
        else:
            return

        await send_telegram_message(msg)
    except Exception as e:
        print(f"[Telegram] notify_async_fill 오류: {e}")
