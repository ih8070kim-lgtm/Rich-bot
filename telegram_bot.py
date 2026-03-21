"""
V10.11b Trinity 관제 봇
==============================
읽기 대상: system_state.json  (runner가 매 루프 갱신하는 평탄 구조)
쓰기 대상: system_state.json  (봇 명령 반영)

v10.7 변경:
  - /status: 롱/숏 마진율 분리, SOFT_HEDGE 배지
  - Pullback 제거 → Breakout 배지
  - /hedge: SOFT_HEDGE 포함
"""
import json
import os
import asyncio
import requests
import time as _time
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv("api.env")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "system_state.json")


# ═════════════════════════════════════════════════════════════════
# JSON 유틸
# ═════════════════════════════════════════════════════════════════
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(data: dict):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        print(f"[Bot] 저장 실패: {e}")


def patch_state(patch: dict):
    s = load_state()
    s.update(patch)
    save_state(s)


async def send_telegram_message(msg: str):
    if not BOT_TOKEN or not CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        await asyncio.to_thread(
            requests.post, url,
            json={"chat_id": CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5.0,
        )
    except Exception as e:
        print(f"[Bot] 발송 실패: {e}")


# ═════════════════════════════════════════════════════════════════
# 포지션 리스트 빌드  (v10.7: SOFT_HEDGE, Breakout 배지)
# ═════════════════════════════════════════════════════════════════
def build_position_list(positions: list):
    long_core  = []
    short_core = []
    hedge_list = []

    for pos in positions:
        sym        = pos.get("symbol", "?")
        side       = pos.get("side", "BUY")
        tier       = int(pos.get("tier", 1) or 1)
        roi        = float(pos.get("roi_pct", 0.0) or 0.0)
        step       = int(pos.get("step", 0) or 0)
        role       = pos.get("role", "")
        entry_type = pos.get("entry_type", "MR")

        roi_icon = "🟢" if roi >= 0 else "🔴"
        trail_tag = "✂️" if step >= 1 else ""

        if role in ("HEDGE", "SOFT_HEDGE", "CORE_HEDGE"):
            h_badge = "🛡️"
            if role == "SOFT_HEDGE": h_badge = "🔰"
            line = f"  {roi_icon}{h_badge} {sym} T{tier}{trail_tag}: {roi:+.2f}%"
            hedge_list.append(line)
        elif role == "INSURANCE_SH":
            line = f"  {roi_icon}🩹 {sym} INS: {roi:+.2f}%"
            if side == "BUY": long_core.append(line)
            else: short_core.append(line)
        elif role == "CORE_BALANCE":
            line = f"  {roi_icon}⚖️ {sym} T{tier}{trail_tag}: {roi:+.2f}%"
            if side == "BUY": long_core.append(line)
            else: short_core.append(line)
        else:
            type_badge = "🚀" if entry_type == "BREAKOUT" else "🔁"
            line = f"  {roi_icon}{type_badge} {sym} T{tier}{trail_tag}: {roi:+.2f}%"
            if side == "BUY": long_core.append(line)
            else: short_core.append(line)

    def _build(items):
        return "\n".join(items) if items else "  └ <i>없음</i>"

    long_text  = _build(long_core)
    short_text = _build(short_core)
    hedge_text = "\n🛡️ <b>Hedge</b>\n" + "\n".join(hedge_list) if hedge_list else ""

    return long_text, short_text, hedge_text, bool(long_core), bool(short_core)


# ═════════════════════════════════════════════════════════════════
# /status  (v10.6: 레짐, 헷지 분리)
# ═════════════════════════════════════════════════════════════════
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    if not s:
        await update.message.reply_text("⏳ 엔진 데이터 수집 중...")
        return

    # 갱신 시각
    try:
        mtime   = os.path.getmtime(STATE_FILE)
        elapsed = int(datetime.now().timestamp() - mtime)
        if elapsed < 60:
            freshness = f"🟢 {elapsed}초 전"
        elif elapsed < 300:
            freshness = f"🟡 {elapsed // 60}분 전"
        else:
            freshness = f"🔴 {elapsed // 60}분 전"
    except Exception:
        freshness = "⚪ 불명"

    total_bal = float(s.get("total_equity",     0.0) or 0.0)
    mr        = float(s.get("margin_ratio",     0.0) or 0.0)
    shutdown  = bool(s.get("shutdown_active",   False))
    baseline  = float(s.get("baseline_balance", total_bal) or total_bal)
    initial   = float(s.get("initial_balance",  total_bal) or total_bal)
    daily_roi = (total_bal - baseline) / baseline * 100 if baseline > 0 else 0.0
    total_roi = (total_bal - initial)  / initial  * 100 if initial  > 0 else 0.0

    # Kill Switch
    if mr >= 0.9:
        ks = f"🔴 동결 (MR {mr*100:.1f}%)"
    elif mr >= 0.8:
        ks = f"🔴 전면차단 (MR {mr*100:.1f}%)"
    elif mr >= 0.7:
        ks = f"🟡 신규차단 (MR {mr*100:.1f}%)"
    else:
        ks = f"🟢 정상 (MR {mr*100:.1f}%)"
    if shutdown:
        ks += f"\n  └ ⛔ DD 셧다운"

    # 레짐
    regime = s.get("regime", "")
    regime_map = {"HIGH": "⚡ HIGH", "NORMAL": "🟢 NORMAL", "LOW": "😴 LOW", "BAD": "💀 BAD"}
    regime_ui = regime_map.get(regime, f"🟢 {regime or '...'}")

    # BTC crash freeze
    crash_until = float(s.get("btc_crash_freeze_until", 0.0) or 0.0)
    crash_active = crash_until > _time.time()
    crash_tag = "  🚨 <b>BTC CRASH FREEZE</b>\n" if crash_active else ""

    # 포지션
    positions = s.get("positions", [])
    long_text, short_text, hedge_text, long_active, short_active = build_position_list(positions)

    long_status  = "💰 보유" if long_active  else "⏳ 대기"
    short_status = "💰 보유" if short_active else "⏳ 대기"
    if not s.get("use_long",  True): long_status  = "🔴 중지"
    if not s.get("use_short", True): short_status = "🔴 중지"

    pos_count = len(positions)

    # ★ v10.7: 롱/숏 마진율 분리
    _bal = total_bal or 1.0
    _lev = 3  # config.LEVERAGE
    _long_margin = sum(
        float(p.get("ep", 0) or 0) * float(p.get("amt", 0) or 0)
        for p in positions if p.get("side") == "BUY"
    ) / _lev / _bal * 100
    _short_margin = sum(
        float(p.get("ep", 0) or 0) * float(p.get("amt", 0) or 0)
        for p in positions if p.get("side") == "SELL"
    ) / _lev / _bal * 100
    _total_margin = _long_margin + _short_margin
    margin_line = f"📊 마진: L {_long_margin:.0f}% | S {_short_margin:.0f}% | T {_total_margin:.0f}%"

    # CORE 카운트 (HEDGE/INSURANCE/BALANCE 제외)
    _exclude_roles = ("HEDGE", "SOFT_HEDGE", "CORE_HEDGE", "INSURANCE_SH", "CORE_BALANCE")
    core_long  = sum(1 for p in positions if p.get("side") == "BUY"  and p.get("role", "") not in _exclude_roles)
    core_short = sum(1 for p in positions if p.get("side") == "SELL" and p.get("role", "") not in _exclude_roles)

    msg = (
        f"<b>Trinity V10.11b</b>  {freshness}\n"
        f"────────────────\n"
        f"⚡ {ks}\n"
        f"🌡️ 레짐: <b>{regime_ui}</b>\n"
        f"{crash_tag}"
        f"💰 <b>${total_bal:,.2f}</b>  📊 일 {daily_roi:+.2f}% | 총 {total_roi:+.2f}%\n"
        f"{margin_line}\n"
        f"────────────────\n"
        f"📈 <b>Long</b> {long_status} ({core_long})\n"
        f"{long_text}\n\n"
        f"📉 <b>Short</b> {short_status} ({core_short})\n"
        f"{short_text}"
        f"{hedge_text}\n"
        f"────────────────\n"
        f"🔁=MR  🛡️=헷지  ⚖️=밸런스  🩹=보험  ✂️=Trail\n"
        f"/status /perf /hedge /regime /emergency"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════
# /perf  (금일 성과)
# ═════════════════════════════════════════════════════════════════
async def perf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import csv
    from datetime import date
    today = date.today().isoformat()
    log_dir = os.path.join(BASE_DIR, "v9_logs")
    trades_path = os.path.join(log_dir, "log_trades.csv")

    stats = {"total": 0, "win": 0, "pnl": 0.0,
             "mr": {"w": 0, "t": 0}, "bo": {"w": 0, "t": 0}, "bal": {"w": 0, "t": 0},
             "t1": {"w":0,"t":0}, "t2": {"w":0,"t":0}, "t3": {"w":0,"t":0}}

    if os.path.exists(trades_path):
        try:
            with open(trades_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if not row.get("time", "").startswith(today):
                        continue
                    try:
                        pnl = float(row.get("pnl_usdt", 0) or 0)
                        tier = max(1, min(3, int(row.get("dca_level", 1) or 1)))
                        etype = row.get("entry_type", "MR") or "MR"
                    except (ValueError, TypeError):
                        continue
                    stats["total"] += 1
                    stats["pnl"] += pnl
                    w = pnl > 0
                    if w: stats["win"] += 1
                    tk = f"t{tier}"
                    stats[tk]["t"] += 1
                    if w: stats[tk]["w"] += 1
                    ek = "bo" if etype == "BREAKOUT" else ("bal" if etype == "BALANCE" else "mr")
                    stats[ek]["t"] += 1
                    if w: stats[ek]["w"] += 1
        except Exception:
            pass

    def _wr(d):
        return f"{d['w']}/{d['t']} ({d['w']/d['t']*100:.0f}%)" if d['t'] > 0 else "N/A"

    total_wr = f"{stats['win']}/{stats['total']} ({stats['win']/stats['total']*100:.0f}%)" if stats['total'] > 0 else "N/A"
    pnl_icon = "🟢" if stats["pnl"] >= 0 else "🔴"

    msg = (
        f"<b>📊 금일 성과</b>\n"
        f"────────────────\n"
        f"거래: {stats['total']}건  승률: {total_wr}\n"
        f"{pnl_icon} PnL: <b>${stats['pnl']:+.2f}</b>\n"
        f"────────────────\n"
        f"<b>티어별</b>\n"
        f"  T1: {_wr(stats['t1'])}  T2: {_wr(stats['t2'])}  T3: {_wr(stats['t3'])}\n"
        f"<b>전략별</b>\n"
        f"  🔁 MR: {_wr(stats['mr'])}  🚀 BO: {_wr(stats['bo'])}  ⚖️ BAL: {_wr(stats['bal'])}\n"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════
# /hedge  (헷지 상태 상세)
# ═════════════════════════════════════════════════════════════════
async def hedge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    positions = s.get("positions", [])

    hedges = [p for p in positions if p.get("role") in ("HEDGE", "SOFT_HEDGE")]
    sources = []
    for h in hedges:
        src_sym = h.get("source_sym", "")
        src = next((p for p in positions if p.get("symbol") == src_sym and p.get("role") not in ("HEDGE", "SOFT_HEDGE")), None)
        sources.append(src)

    if not hedges:
        await update.message.reply_text(
            "🛡️ <b>Hedge 현황</b>\n────────────────\n활성 헷지 없음",
            parse_mode="HTML"
        )
        return

    lines = []
    for h, src in zip(hedges, sources):
        h_sym = h.get("symbol", "?")
        h_side = "L" if h.get("side") == "BUY" else "S"
        h_roi = float(h.get("roi_pct", 0) or 0)
        h_tier = int(h.get("tier", 1) or 1)
        h_icon = "🟢" if h_roi >= 0 else "🔴"
        h_role = h.get("role", "HEDGE")
        h_badge = "🛡️" if h_role == "HEDGE" else "🔰"

        src_roi = float(src.get("roi_pct", 0) or 0) if src else 0
        src_tier = int(src.get("tier", 1) or 1) if src else 0
        src_tag = f"소스 T{src_tier} {src_roi:+.2f}%" if src else "소스 청산됨"

        lines.append(
            f"  {h_icon}{h_badge} {h_sym} [{h_side}] T{h_tier}: {h_roi:+.2f}%\n"
            f"     └ {src_tag}"
        )

    msg = (
        f"🛡️ <b>Hedge 현황</b> ({len(hedges)}건)\n"
        f"────────────────\n"
        + "\n".join(lines)
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════
# /regime  (레짐 + SKEW 상세)
# ═════════════════════════════════════════════════════════════════
async def regime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    mr = float(s.get("margin_ratio", 0) or 0)
    regime = s.get("regime", "?")
    regime_map = {"HIGH": "⚡ HIGH", "NORMAL": "🟢 NORMAL", "LOW": "😴 LOW", "BAD": "💀 BAD"}
    regime_ui = regime_map.get(regime, regime)

    positions = s.get("positions", [])
    long_mr = sum(float(p.get("roi_pct", 0) or 0) != 0 and p.get("side") == "BUY"
                  and p.get("role", "") != "HEDGE" for p in positions)
    short_mr = sum(float(p.get("roi_pct", 0) or 0) != 0 and p.get("side") == "SELL"
                   and p.get("role", "") != "HEDGE" for p in positions)

    crash_until = float(s.get("btc_crash_freeze_until", 0.0) or 0.0)
    crash_remain = max(0, int(crash_until - _time.time()))
    crash_line = f"🚨 BTC Crash Freeze: {crash_remain}초 남음\n" if crash_remain > 0 else ""

    msg = (
        f"🌡️ <b>레짐 상세</b>\n"
        f"────────────────\n"
        f"BTC 변동성: <b>{regime_ui}</b>\n"
        f"마진율: {mr*100:.1f}%\n"
        f"CORE 포지션: L{long_mr} / S{short_mr}\n"
        f"{crash_line}"
        f"────────────────\n"
        f"ATR 배수: {'N/A(차단)' if regime=='BAD' else '3.6' if regime=='LOW' else '2.8' if regime=='NORMAL' else '2.4'}\n"
        f"Breakout: {'✅ vol1.5×' if regime=='HIGH' else '✅ vol2.0×' if regime=='NORMAL' else '❌ 비활성'}\n"
        f"ZOMBIE: {'⏸ 스킵' if regime=='HIGH' else '⏩ 30%' if regime=='BAD' else '⏩ 50%' if regime=='LOW' else '✅ 기본'}\n"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════
# /emergency  (전체 청산 — 2단계)
# ═════════════════════════════════════════════════════════════════
_PENDING_CLOSE: dict = {}


async def emergency_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    pending = _PENDING_CLOSE.get(chat_id)

    # 이미 대기 중이면 실행
    if pending and _time.time() < pending["expires"]:
        mode = pending["mode"]
        _PENDING_CLOSE.pop(chat_id, None)
        patch_state({"close_all_mode": mode, "close_all_requested": True})
        label = "LIMIT" if mode == "limit" else "MARKET"
        await update.message.reply_text(
            f"🚨 <b>전체 청산 실행 — {label}</b>",
            parse_mode="HTML",
        )
        return

    # 모드 결정: /emergency market 또는 기본 limit
    mode = "market" if context.args and context.args[0].lower() == "market" else "limit"
    _PENDING_CLOSE[chat_id] = {"mode": mode, "expires": _time.time() + 30}
    label = "MARKET (즉시)" if mode == "market" else "LIMIT (지정가→시장가)"
    await update.message.reply_text(
        f"⚠️ <b>전체 청산 — {label}</b>\n"
        f"30초 안에 /emergency 다시 입력하면 실행\n"
        f"무시하면 자동 취소",
        parse_mode="HTML",
    )


# ═════════════════════════════════════════════════════════════════
# 방향 토글
# ═════════════════════════════════════════════════════════════════
async def control_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd    = update.message.text.lower().strip("/")
    target = "long" if "long" in cmd else ("short" if "short" in cmd else None)
    if not target:
        return
    is_on = cmd.startswith("used_")
    patch_state({f"use_{target}": is_on})
    status = "🟢 활성화" if is_on else "🔴 중지"
    await update.message.reply_text(
        f"✅ <b>{target.upper()}</b>: {status}", parse_mode="HTML",
    )


# ═════════════════════════════════════════════════════════════════
# 기타 명령
# ═════════════════════════════════════════════════════════════════
async def unlock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    patch_state({
        "shutdown_active": False,
        "shutdown_until":  0.0,
        "shutdown_reason": "",
        "is_locked":       False,
        "baseline_date":   "",
    })
    await update.message.reply_text("✅ <b>셧다운 해제 완료</b>", parse_mode="HTML")


async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_dir = os.path.join(BASE_DIR, "v9_logs")
    fills   = os.path.join(log_dir, "log_fills.csv")
    resp    = "📜 <b>최근 체결</b>\n\n"
    if os.path.exists(fills) and os.path.getsize(fills) > 0:
        with open(fills, "r", encoding="utf-8") as f:
            for line in f.readlines()[-5:]:
                resp += f"- {line.strip()}\n"
    else:
        resp += "- 기록 없음\n"
    await update.message.reply_text(resp, parse_mode="HTML")


# ═════════════════════════════════════════════════════════════════
# 메인
# ═════════════════════════════════════════════════════════════════
def main():
    print("📢 Trinity V10.11b 관제 봇 가동")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    app.add_handler(CommandHandler("status",    status_command))
    app.add_handler(CommandHandler("perf",      perf_command))
    app.add_handler(CommandHandler("hedge",     hedge_command))
    app.add_handler(CommandHandler("regime",    regime_command))
    app.add_handler(CommandHandler("log",       log_command))
    app.add_handler(CommandHandler("unlock",    unlock_command))
    app.add_handler(CommandHandler("emergency", emergency_command))
    for strat in ["long", "short"]:
        app.add_handler(CommandHandler(f"used_{strat}",   control_strategy))
        app.add_handler(CommandHandler(f"unused_{strat}", control_strategy))
    app.run_polling()


if __name__ == "__main__":
    main()
