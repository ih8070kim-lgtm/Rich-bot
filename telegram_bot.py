"""
V10.13 Trinity 관제 봇  (최종 아키텍처 반영)
=============================================
변경:
  - /regime: Breakout 제거, ATR 배수 실제값, BAD 제거
  - build_position_list: T5_MINI 뱃지 추가
  - /closeall: 긴급 전량청산
  - /status: 헷지 슬롯 1개 제한 표시
"""
import json, os, asyncio, requests, time as _time, csv
from datetime import datetime, date
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv("api.env")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "system_state.json")


# ═══════════════════════════════════════════════════════════════════
# JSON 유틸
# ═══════════════════════════════════════════════════════════════════
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
    s = load_state(); s.update(patch); save_state(s)

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


# ═══════════════════════════════════════════════════════════════════
# 포지션 리스트 빌드
# ═══════════════════════════════════════════════════════════════════
def build_position_list(positions: list):
    long_core, short_core, hedge_list = [], [], []

    for pos in positions:
        sym        = pos.get("symbol", "?")
        side       = pos.get("side", "BUY")
        tier       = int(pos.get("tier", 1) or 1)
        roi        = float(pos.get("roi_pct", 0.0) or 0.0)
        step       = int(pos.get("step", 0) or 0)
        role       = pos.get("role", "")
        entry_type = pos.get("entry_type", "MR")
        mini       = bool(pos.get("t5_mini_active", False))

        roi_icon  = "🟢" if roi >= 0 else "🔴"
        trail_tag = "✂️" if step >= 1 else ""
        mini_tag  = "🎯" if mini else ""  # T5 미니게임

        if role in ("HEDGE", "SOFT_HEDGE", "CORE_HEDGE"):
            h_badge = "🛡️"
            line = f"  {roi_icon}{h_badge} {sym} T{tier}{trail_tag}{mini_tag}: {roi:+.2f}%"
            hedge_list.append(line)
        elif role == "INSURANCE_SH":
            line = f"  {roi_icon}🩹 {sym} INS: {roi:+.2f}%"
            if side == "BUY": long_core.append(line)
            else: short_core.append(line)
        else:
            # CORE_MR, CORE_BREAKOUT, BALANCE 등
            type_badge = "🔁"
            line = f"  {roi_icon}{type_badge} {sym} T{tier}{trail_tag}{mini_tag}: {roi:+.2f}%"
            if side == "BUY": long_core.append(line)
            else: short_core.append(line)

    def _build(items):
        return "\n".join(items) if items else "  └ <i>없음</i>"

    hedge_text = "\n🛡️ <b>Hedge</b>\n" + "\n".join(hedge_list) if hedge_list else ""
    return _build(long_core), _build(short_core), hedge_text, bool(long_core), bool(short_core)


# ═══════════════════════════════════════════════════════════════════
# /status
# ═══════════════════════════════════════════════════════════════════
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    if not s:
        await update.message.reply_text("⏳ 엔진 데이터 수집 중...")
        return

    try:
        mtime   = os.path.getmtime(STATE_FILE)
        elapsed = int(datetime.now().timestamp() - mtime)
        freshness = ("🟢" if elapsed<60 else "🟡" if elapsed<300 else "🔴") + \
                    (f" {elapsed}초 전" if elapsed<60 else f" {elapsed//60}분 전")
    except Exception:
        freshness = "⚪ 불명"

    total_bal = float(s.get("total_equity",     0.0) or 0.0)
    mr        = float(s.get("margin_ratio",     0.0) or 0.0)
    shutdown  = bool(s.get("shutdown_active",   False))
    baseline  = float(s.get("baseline_balance", total_bal) or total_bal)
    initial   = float(s.get("initial_balance",  total_bal) or total_bal)
    daily_roi = (total_bal - baseline) / baseline * 100 if baseline > 0 else 0.0
    total_roi = (total_bal - initial)  / initial  * 100 if initial  > 0 else 0.0

    if mr >= 0.9:   ks = f"🔴 동결 (MR {mr*100:.1f}%)"
    elif mr >= 0.8: ks = f"🔴 전면차단 (MR {mr*100:.1f}%)"
    elif mr >= 0.7: ks = f"🟡 신규차단 (MR {mr*100:.1f}%)"
    else:           ks = f"🟢 정상 (MR {mr*100:.1f}%)"
    if shutdown:    ks += "\n  └ ⛔ DD 셧다운"

    regime    = s.get("regime", "")
    regime_map = {"HIGH": "⚡ HIGH", "NORMAL": "🟢 NORMAL", "LOW": "😴 LOW"}
    regime_ui = regime_map.get(regime, f"🟢 {regime or '...'}")

    crash_until  = float(s.get("btc_crash_freeze_until", 0.0) or 0.0)
    crash_active = crash_until > _time.time()
    crash_tag    = "  🚨 <b>BTC CRASH FREEZE</b>\n" if crash_active else ""

    positions = s.get("positions", [])
    long_text, short_text, hedge_text, long_active, short_active = build_position_list(positions)

    long_status  = ("💰 보유" if long_active  else "⏳ 대기")
    short_status = ("💰 보유" if short_active else "⏳ 대기")
    if not s.get("use_long",  True): long_status  = "🔴 중지"
    if not s.get("use_short", True): short_status = "🔴 중지"

    _bal = total_bal or 1.0; _lev = 3
    _exclude = ("HEDGE","SOFT_HEDGE","CORE_HEDGE","INSURANCE_SH","CORE_BALANCE")
    _long_margin  = sum(float(p.get("ep",0) or 0)*float(p.get("amt",0) or 0)
                        for p in positions if p.get("side")=="BUY") / _lev / _bal * 100
    _short_margin = sum(float(p.get("ep",0) or 0)*float(p.get("amt",0) or 0)
                        for p in positions if p.get("side")=="SELL") / _lev / _bal * 100
    core_long  = sum(1 for p in positions if p.get("side")=="BUY"  and p.get("role","") not in _exclude)
    core_short = sum(1 for p in positions if p.get("side")=="SELL" and p.get("role","") not in _exclude)
    hedge_cnt  = sum(1 for p in positions if p.get("role","") in ("CORE_HEDGE","HEDGE","SOFT_HEDGE"))
    # 최종 아키텍처: 헷지 최대 1개
    hedge_status = f"🛡️ 헷지: {hedge_cnt}/1" if hedge_cnt > 0 else ""

    msg = (
        f"<b>Trinity V10.13</b>  {freshness}\n"
        f"────────────────\n"
        f"⚡ {ks}\n"
        f"🌡️ 레짐: <b>{regime_ui}</b>\n"
        f"{crash_tag}"
        f"💰 <b>${total_bal:,.2f}</b>  📊 일 {daily_roi:+.2f}% | 총 {total_roi:+.2f}%\n"
        f"📊 마진: L {_long_margin:.0f}% | S {_short_margin:.0f}%\n"
        f"────────────────\n"
        f"📈 <b>Long</b> {long_status} ({core_long})\n"
        f"{long_text}\n\n"
        f"📉 <b>Short</b> {short_status} ({core_short})\n"
        f"{short_text}"
        f"{hedge_text}\n"
        f"{('  '+hedge_status+chr(10)) if hedge_status else ''}"
        f"────────────────\n"
        f"🔁=MR  🛡️=헷지  🩹=보험  🎯=T5미니  ✂️=Trail\n"
        f"/status /perf /regime /unlock /closeall"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
# /perf
# ═══════════════════════════════════════════════════════════════════
async def perf_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    today = date.today().isoformat()
    log_dir = os.path.join(BASE_DIR, "v9_logs")
    trades_path = os.path.join(log_dir, "log_trades.csv")

    stats = {
        "total":0,"win":0,"pnl":0.0,
        "t1":{"w":0,"t":0,"pnl":0},"t2":{"w":0,"t":0,"pnl":0},
        "t3":{"w":0,"t":0,"pnl":0},"t4":{"w":0,"t":0,"pnl":0},
        "t5":{"w":0,"t":0,"pnl":0},
        "hedge":{"w":0,"t":0,"pnl":0},"ins":{"w":0,"t":0,"pnl":0},
        "core":{"w":0,"t":0,"pnl":0},
    }

    if os.path.exists(trades_path):
        try:
            with open(trades_path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    if not row.get("time","").startswith(today):
                        continue
                    try:
                        pnl  = float(row.get("pnl_usdt",0) or 0)
                        tier = max(1, min(5, int(row.get("dca_level",1) or 1)))
                        role = row.get("role","CORE_MR") or "CORE_MR"
                    except (ValueError, TypeError):
                        continue
                    stats["total"]+=1; stats["pnl"]+=pnl
                    w = pnl>0
                    if w: stats["win"]+=1
                    tk=f"t{tier}"
                    if tk in stats:
                        stats[tk]["t"]+=1; stats[tk]["pnl"]+=pnl
                        if w: stats[tk]["w"]+=1
                    if role=="CORE_HEDGE":
                        stats["hedge"]["t"]+=1; stats["hedge"]["pnl"]+=pnl
                        if w: stats["hedge"]["w"]+=1
                    elif role=="INSURANCE_SH":
                        stats["ins"]["t"]+=1; stats["ins"]["pnl"]+=pnl
                        if w: stats["ins"]["w"]+=1
                    else:
                        stats["core"]["t"]+=1; stats["core"]["pnl"]+=pnl
                        if w: stats["core"]["w"]+=1
        except Exception:
            pass

    def _wr(d):
        return f"{d['w']}/{d['t']}({d['w']/d['t']*100:.0f}%)" if d['t'] else "—"
    def _pnl(d):
        return f" ${d['pnl']:+.1f}" if d['t'] else ""

    total_wr = f"{stats['win']}/{stats['total']}({stats['win']/stats['total']*100:.0f}%)" \
               if stats['total'] else "N/A"
    pnl_icon = "🟢" if stats["pnl"]>=0 else "🔴"

    msg = (
        f"<b>📊 금일 성과</b>\n"
        f"────────────────\n"
        f"거래: {stats['total']}건  승률: {total_wr}\n"
        f"{pnl_icon} PnL: <b>${stats['pnl']:+.2f}</b>\n"
        f"────────────────\n"
        f"<b>티어별</b>\n"
        f"  T1: {_wr(stats['t1'])}{_pnl(stats['t1'])}\n"
        f"  T2: {_wr(stats['t2'])}{_pnl(stats['t2'])}\n"
        f"  T3: {_wr(stats['t3'])}{_pnl(stats['t3'])}\n"
        f"  T4: {_wr(stats['t4'])}{_pnl(stats['t4'])}\n"
        f"  T5: {_wr(stats['t5'])}{_pnl(stats['t5'])}\n"
        f"<b>역할별</b>\n"
        f"  🔁 CORE: {_wr(stats['core'])}{_pnl(stats['core'])}\n"
        f"  🛡️ 헷지: {_wr(stats['hedge'])}{_pnl(stats['hedge'])}\n"
        f"  🩹 보험: {_wr(stats['ins'])}{_pnl(stats['ins'])}\n"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
# /regime
# ═══════════════════════════════════════════════════════════════════
async def regime_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s  = load_state()
    mr = float(s.get("margin_ratio",0) or 0)
    regime = s.get("regime","?")
    regime_map = {"HIGH":"⚡ HIGH","NORMAL":"🟢 NORMAL","LOW":"😴 LOW"}
    regime_ui  = regime_map.get(regime, regime)

    positions = s.get("positions",[])
    _exclude  = ("HEDGE","SOFT_HEDGE","CORE_HEDGE","INSURANCE_SH")
    core_long  = sum(1 for p in positions if p.get("side")=="BUY"  and p.get("role","") not in _exclude)
    core_short = sum(1 for p in positions if p.get("side")=="SELL" and p.get("role","") not in _exclude)

    crash_until  = float(s.get("btc_crash_freeze_until",0.0) or 0.0)
    crash_remain = max(0, int(crash_until - _time.time()))
    crash_line   = f"🚨 BTC Crash Freeze: {crash_remain}초 남음\n" if crash_remain>0 else ""

    # 실제 config 값 기반 (NORMAL=2.4, HIGH=+0.6=3.0)
    atr_map  = {"HIGH":"3.0(+0.6)", "NORMAL":"2.4", "LOW":"2.4"}
    atr_val  = atr_map.get(regime, "2.4")

    # ZOMBIE: HIGH=스킵, NORMAL/LOW=활성
    zombie_map = {"HIGH":"⏸ 스킵(HIGH)", "NORMAL":"✅ 활성", "LOW":"✅ 활성"}
    zombie_val = zombie_map.get(regime, "✅ 활성")

    msg = (
        f"🌡️ <b>레짐 상세</b>\n"
        f"────────────────\n"
        f"BTC 변동성: <b>{regime_ui}</b>\n"
        f"마진율: {mr*100:.1f}%\n"
        f"CORE 포지션: L{core_long} / S{core_short}\n"
        f"{crash_line}"
        f"────────────────\n"
        f"Long ATR 배수: {atr_val}×\n"
        f"Short ATR 배수: {atr_val.replace('3.0','2.4').replace('+0.6','')}\n"
        f"ZOMBIE: {zombie_val}\n"
        f"DCA 간격: -8.25% ROI (전 레짐 동일)\n"
        f"헷지 슬롯: 최대 1 (tail-risk 보험)\n"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
# /closeall  — 긴급 전량청산
# ═══════════════════════════════════════════════════════════════════
async def closeall_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args or []
    if not args or args[0].lower() != "confirm":
        await update.message.reply_text(
            "⚠️ <b>긴급 전량청산</b>\n"
            "모든 포지션을 시장가로 즉시 청산합니다.\n\n"
            "확인하려면:\n<code>/closeall confirm</code>",
            parse_mode="HTML"
        )
        return

    patch_state({"close_all_requested": True, "close_all_mode": "market"})
    await update.message.reply_text(
        "✅ <b>전량청산 요청됨</b>\n"
        "다음 틱(~10초)에 시장가 전량청산 실행됩니다.",
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════
# /unlock
# ═══════════════════════════════════════════════════════════════════
async def unlock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = load_state()
    was_active = bool(s.get("shutdown_active", False))
    reason = s.get("shutdown_reason","")
    bal    = float(s.get("total_equity",0) or 0)

    patch_state({
        "shutdown_active": False,
        "shutdown_until":  0.0,
        "shutdown_reason": "",
        "is_locked":       False,
        "baseline_date":   "",
    })

    msg = (
        f"✅ <b>DD 셧다운 해제</b>\n"
        f"────────────────\n"
        f"사유: {reason or '수동 해제'}\n"
        f"현재 잔고: ${bal:,.2f}\n"
        f"baseline 리셋됨 (다음 틱 갱신)"
        if was_active else "ℹ️ 셧다운 상태 아님 (baseline 리셋됨)"
    )
    await update.message.reply_text(msg, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
# /log
# ═══════════════════════════════════════════════════════════════════
async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log_dir = os.path.join(BASE_DIR, "v9_logs")
    fills   = os.path.join(log_dir, "log_fills.csv")
    resp    = "📜 <b>최근 체결</b>\n\n"
    if os.path.exists(fills) and os.path.getsize(fills)>0:
        with open(fills,"r",encoding="utf-8") as f:
            for line in f.readlines()[-5:]:
                resp += f"- {line.strip()}\n"
    else:
        resp += "- 기록 없음\n"
    await update.message.reply_text(resp, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
# 방향 토글
# ═══════════════════════════════════════════════════════════════════
async def control_strategy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmd    = update.message.text.lower().strip("/")
    target = "long" if "long" in cmd else ("short" if "short" in cmd else None)
    if not target: return
    is_on = cmd.startswith("used_")
    patch_state({f"use_{target}": is_on})
    status = "🟢 활성화" if is_on else "🔴 중지"
    await update.message.reply_text(f"✅ <b>{target.upper()}</b>: {status}", parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════
def main():
    print("📢 Trinity V10.13 관제 봇 가동")
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .build()
    )
    app.add_handler(CommandHandler("status",   status_command))
    app.add_handler(CommandHandler("perf",     perf_command))
    app.add_handler(CommandHandler("regime",   regime_command))
    app.add_handler(CommandHandler("log",      log_command))
    app.add_handler(CommandHandler("unlock",   unlock_command))
    app.add_handler(CommandHandler("closeall", closeall_command))
    for strat in ["long","short"]:
        app.add_handler(CommandHandler(f"used_{strat}",   control_strategy))
        app.add_handler(CommandHandler(f"unused_{strat}", control_strategy))
    app.run_polling()


if __name__ == "__main__":
    main()
