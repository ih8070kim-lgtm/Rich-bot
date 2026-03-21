import asyncio
import json
import math
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

# 💡 [V8.9.6] 텔레그램 엔진 임포트 (미스매치 교차 검증 알림용)
try:
    from telegram_engine import send_telegram_message
except ImportError:

    async def send_telegram_message(msg):
        print(f"텔레그램 발송 대기: {msg}")


# ==========================================
# 1. API 및 환경 설정 (V8.9.6 모델 이원화 클러스터)
# ==========================================
load_dotenv("api.env")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
LLM_TIMEOUT_SECONDS = 15.0
DB_PATH = "ai_history.sqlite"
RSS_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# 💡 [V8.9.6] 장세 판독용: 하이쿠 4.5 메인 (기계적 채점 엄수)
# V8.9.58: Primary model fixed to claude-3-5-sonnet-20240620
FALLBACK_MODELS = [
    "claude-3-5-sonnet-20240620",  # Primary (V8.9.58 spec)
    "claude-haiku-4-5",  # Fallback 1
    "claude-sonnet-4-6",  # Fallback 2
]

PERSONA_MODEL = "claude-3-5-sonnet-20240620"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sentiment_log (
                timestamp REAL PRIMARY KEY,
                score_raw REAL
            )
        """)
        conn.commit()
    finally:
        conn.close()


init_db()


# ==========================================
# 2. 통신 에러 전용 로깅 모듈
# ==========================================
def log_api_error(error_msg):
    kst_now = datetime.utcnow() + timedelta(hours=9)
    time_str = kst_now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open("api_error_log.txt", "a", encoding="utf-8") as f:
            f.write(f"[{time_str}] {error_msg}\n")
    except Exception:
        pass


# ==========================================
# 3. RSS 캐싱 및 서머타임(DST) 연산 모듈
# ==========================================
last_rss_fetch_time = 0
dynamic_events = []


def is_us_dst(dt_utc):
    year = dt_utc.year
    march_1st = datetime(year, 3, 1)
    nov_1st = datetime(year, 11, 1)

    march_2nd_sun = march_1st + timedelta(days=(6 - march_1st.weekday() + 7) % 7 + 7)
    nov_1st_sun = nov_1st + timedelta(days=(6 - nov_1st.weekday() + 7) % 7)

    dt_naive = dt_utc.replace(tzinfo=None)
    return march_2nd_sun <= dt_naive < nov_1st_sun


def _fetch_rss_events_sync():
    global last_rss_fetch_time, dynamic_events
    now = time.time()

    if now - last_rss_fetch_time < 3600:
        return

    try:
        resp = requests.get(RSS_FEED_URL, timeout=5)
        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            events = []
            for item in root.findall("event"):
                country = item.find("country").text
                impact = item.find("impact").text

                if country != "USD":
                    continue

                title = item.find("title").text
                is_speech = bool(
                    re.search(
                        r"(Chair|President|FOMC|Speaks|Testimony|Biden|Trump|Harris|SEC|Gensler)",
                        title,
                        re.IGNORECASE,
                    )
                )

                if impact == "High" or is_speech:
                    date_str = item.find("date").text
                    time_str = item.find("time").text

                    if not time_str or time_str.lower() in ["all day", "tentative"]:
                        continue

                    dt_et_str = f"{date_str} {time_str}"
                    dt_et = datetime.strptime(dt_et_str, "%m-%d-%Y %I:%M%p")

                    et_offset = 4 if is_us_dst(dt_et + timedelta(hours=14)) else 5
                    dt_kst = dt_et + timedelta(hours=et_offset) + timedelta(hours=9)

                    events.append({"title": title, "time": dt_kst, "is_speech": is_speech})
            dynamic_events = events
            last_rss_fetch_time = now
    except Exception:
        pass


# ==========================================
# 4. 안전망, 시간 판독 및 뉴스 메인 평가 로직
# ==========================================
def get_safe_fallback():
    return {
        "score_raw": 50.0,
        "score_ema_3m": 50.0,
        "velocity_3m": 0.0,
        "slope_60m": 0.0,
        "ai_volatility": 0.0,
        "market_mode": "NORMAL",
    }


def get_market_mode(current_velocity=0.0, rho_5m=0.0, btc_dev=0.0):
    if abs(current_velocity) >= 5.0:
        return "HIGH_VOL"

    base_mode = "NORMAL"
    if rho_5m <= -0.5:
        base_mode = "TRAP"
    elif rho_5m >= 0.8 and abs(current_velocity) >= 3.0:
        base_mode = "HIGH_VOL"
    elif btc_dev <= -2.0:
        base_mode = "BEAR"
    elif btc_dev >= 2.0:
        base_mode = "BULL"
    elif rho_5m < 0.3 and abs(current_velocity) < 3.0:
        base_mode = "LOW_VOL"

    now_kst = datetime.utcnow() + timedelta(hours=9)
    time_str = now_kst.strftime("%H:%M")

    is_event_high_vol = False
    if "08:50" <= time_str <= "09:30":
        is_event_high_vol = True

    if is_us_dst(datetime.utcnow()):
        if "21:15" <= time_str <= "23:00":
            is_event_high_vol = True
    else:
        if "22:15" <= time_str <= "23:59":
            is_event_high_vol = True

    for ev in dynamic_events:
        time_diff_mins = (now_kst - ev["time"]).total_seconds() / 60.0

        if ev["is_speech"]:
            if -15 <= time_diff_mins <= 60:
                is_event_high_vol = True
            elif 60 < time_diff_mins <= 120:
                if abs(current_velocity) >= 0.5:
                    is_event_high_vol = True
        else:
            if -10 <= time_diff_mins <= 30:
                is_event_high_vol = True

    if is_event_high_vol and base_mode != "TRAP":
        return "HIGH_VOL"

    return base_mode


def _call_anthropic_sync(headers, payload, target_models=None):
    models_to_try = target_models if target_models else FALLBACK_MODELS

    if not headers.get("x-api-key"):
        fatal_msg = "🚨 [FATAL] API 키가 설정되지 않았습니다. api.env 파일을 확인하십시오."
        print(fatal_msg)
        log_api_error(fatal_msg)
        return {"status": "FATAL_ERROR"}

    for idx, model_name in enumerate(models_to_try, 1):
        payload["model"] = model_name

        try:
            resp = requests.post(
                ANTHROPIC_API_URL, headers=headers, json=payload, timeout=LLM_TIMEOUT_SECONDS
            )
            if resp.status_code == 200:
                if idx > 1 and not target_models:
                    print(f"🔄 [AI Engine] {idx}차 방어 모델({model_name})로 우회 통신 성공.")
                return resp.json()

            error_msg = f"HTTP {resp.status_code}: {resp.text}"
            print(f"⚠️ [Anthropic API Error - {model_name}] {error_msg}")

            resp_text_lower = resp.text.lower()
            if (
                "credit" in resp_text_lower
                or "balance" in resp_text_lower
                or resp.status_code == 402
            ):
                fatal_msg = "🚨 [FATAL ALERT] Anthropic API 잔고(Credit) 소진. 봇 가동을 즉시 중단해야 합니다."
                print(fatal_msg)
                log_api_error(fatal_msg)
                return {"status": "FATAL_ERROR"}
            else:
                log_api_error(f"[{model_name} 우회] {error_msg}")
                continue

        except requests.exceptions.RequestException as e:
            error_msg = f"Network Exception on {model_name}: {str(e)}"
            print(f"⚠️ [Anthropic 통신 실패 - {model_name}] {error_msg}")
            log_api_error(error_msg)
            continue

    print("🚨 [AI Engine] 통신 응답 불가. FATAL_ERROR 발동.")
    return {"status": "FATAL_ERROR"}


async def get_consensus_score(news_json_str, btc_15m_change=0.0, rho_5m=0.0, btc_dev=0.0):
    current_time = time.time()
    await asyncio.to_thread(_fetch_rss_events_sync)

    current_market_mode = get_market_mode(0.0, rho_5m, btc_dev)

    try:
        data = json.loads(news_json_str)
    except Exception as e:
        print(f"🚨 [AI Engine] 입력 파싱 에러: {e}")
        fallback = get_safe_fallback()
        fallback["market_mode"] = current_market_mode
        return fallback

    if data.get("status") == "fail" or data.get("integrity_warning") is True:
        fallback = get_safe_fallback()
        fallback["market_mode"] = current_market_mode
        return fallback

    active_categories = {}
    analysis_text = ""
    categories = data.get("categories", {})

    for cat_name, cat_data in categories.items():
        if cat_data.get("is_active"):
            active_categories[cat_name] = cat_data.get("weight")
            analysis_text += (
                f"<{cat_name.upper()}>\n{cat_data.get('text')}\n</{cat_name.upper()}>\n\n"
            )

    if not active_categories:
        fallback = get_safe_fallback()
        fallback["market_mode"] = current_market_mode
        return fallback

    system_prompt = """
    You are an expert crypto quantitative analyst. Analyze the provided news data categories.
    Assign a sentiment score from 0.0 to 100.0.
    
    [Strict Scoring Rubric]
    80-100: Extreme Bullish
    60-79: Bullish
    45-55: Neutral (Default is 50.0)
    30-44: Bearish
    0-29: Extreme Bearish
    
    Missing categories must be 50.0. Output ONLY valid JSON matching this structure:
    {"policy": score, "retail": score, "data": score}
    Do not include any other text or markdown formatting.
    """

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "max_tokens": 500,
        "system": system_prompt,
        "messages": [{"role": "user", "content": analysis_text}],
        "temperature": 0.0,
    }

    llm_response_json = {}
    try:
        result = await asyncio.to_thread(_call_anthropic_sync, headers, payload)

        if result and result.get("status") == "FATAL_ERROR":
            return {"status": "FATAL_ERROR"}

        if result and "content" in result:
            raw_text = result["content"][0]["text"].strip()
            raw_text = raw_text.replace("```json", "").replace("```", "").strip()
            if not raw_text.startswith("{"):
                match = re.search(r"\{.*\}", raw_text, re.DOTALL)
                if match:
                    raw_text = match.group(0)
                else:
                    raw_text = "{" + raw_text + "}"

            try:
                llm_response_json = json.loads(raw_text)
            except json.JSONDecodeError:
                print("🚨 [AI Engine] JSON 변환 실패. 킬스위치 가동.")
                return {"status": "FATAL_ERROR"}
        else:
            return {"status": "FATAL_ERROR"}
    except Exception as e:
        print(f"🚨 [AI Engine] 통신 장애. 킬스위치 가동: {e}")
        return {"status": "FATAL_ERROR"}

    active_total_weight = data.get("active_total_weight", 1.0)
    if active_total_weight <= 0.0:
        fallback = get_safe_fallback()
        fallback["market_mode"] = current_market_mode
        return fallback

    weighted_sum = 0.0
    for cat_name, weight in active_categories.items():
        raw_score = llm_response_json.get(cat_name, 50.0)
        raw_score = max(0.0, min(100.0, float(raw_score)))
        weighted_sum += raw_score * weight

    score_raw = round(weighted_sum / active_total_weight, 2)

    conn = sqlite3.connect(DB_PATH)
    rows = []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sentiment_log (timestamp, score_raw) VALUES (?, ?)",
            (current_time, score_raw),
        )
        cursor.execute("DELETE FROM sentiment_log WHERE timestamp < ?", (current_time - 86400,))
        cursor.execute(
            "SELECT timestamp, score_raw FROM sentiment_log WHERE timestamp >= ? ORDER BY timestamp ASC",
            (current_time - 3600,),
        )
        rows = cursor.fetchall()
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

    result_dict = {
        "score_raw": score_raw,
        "score_ema_3m": score_raw,
        "velocity_3m": 0.0,
        "slope_60m": 0.0,
        "ai_volatility": 0.0,
        "market_mode": "NORMAL",
    }

    if rows and len(rows) > 1:
        alpha = 0.2
        ema = rows[0][1]
        ema_history = [(rows[0][0], ema)]

        for t, s in rows[1:]:
            ema = (s * alpha) + (ema * (1 - alpha))
            ema_history.append((t, ema))

        current_ema = ema_history[-1][1]
        result_dict["score_ema_3m"] = round(current_ema, 2)

        time_3m_ago = current_time - 180
        past_ema_3m = current_ema
        for t, e in reversed(ema_history):
            if t <= time_3m_ago:
                past_ema_3m = e
                break
        result_dict["velocity_3m"] = round(current_ema - past_ema_3m, 2)

        oldest_time, oldest_score = rows[0]
        time_diff_min = (current_time - oldest_time) / 60.0
        if time_diff_min >= 1.0:
            result_dict["slope_60m"] = round((score_raw - oldest_score) / time_diff_min, 4)

        time_10m_ago = current_time - 600
        scores_10m = [r[1] for r in rows if r[0] >= time_10m_ago]
        if len(scores_10m) > 1:
            mean = sum(scores_10m) / len(scores_10m)
            variance = sum((x - mean) ** 2 for x in scores_10m) / (len(scores_10m) - 1)
            result_dict["ai_volatility"] = round(math.sqrt(variance), 2)

    result_dict["market_mode"] = get_market_mode(result_dict["velocity_3m"], rho_5m, btc_dev)

    return result_dict


# ==========================================
# 5. 전략 C/E/F 분리형 스나이퍼 페르소나 (V8.9 Master)
# ==========================================
async def get_sniper_decision(
    symbol,
    side,
    current_price,
    ohlcv_15m,
    rsi_1m,
    ai_velocity,
    step_num=1,
    curr_roi=0.0,
    strat_type="C",
):

    # 💡 [V8.9.6] AI 점수 반등(Velocity) 기반 진입 하드-락(Hard Lock)
    if curr_roi > -0.05:
        if strat_type == "C":
            if step_num == 3:
                if side.upper() == "BUY" and ai_velocity <= 1.0:
                    print(
                        f"🚫 [Hard Lock] C 3차 BUY 기각: 강력한 반등 모멘텀 부족 (Velocity: {ai_velocity:.2f} <= +1.0)"
                    )
                    return {"decision": "HOLD", "confidence": 0}
                elif side.upper() == "SELL" and ai_velocity >= -1.0:
                    print(
                        f"🚫 [Hard Lock] C 3차 SELL 기각: 강력한 하락 모멘텀 부족 (Velocity: {ai_velocity:.2f} >= -1.0)"
                    )
                    return {"decision": "HOLD", "confidence": 0}
            else:
                if side.upper() == "BUY" and ai_velocity < -0.5:
                    print(
                        f"🚫 [Hard Lock] C BUY 기각: 하락세 미진정 (Velocity: {ai_velocity:.2f} < -0.5)"
                    )
                    return {"decision": "HOLD", "confidence": 0}
                elif side.upper() == "SELL" and ai_velocity > 0.5:
                    print(
                        f"🚫 [Hard Lock] C SELL 기각: 상승세 미진정 (Velocity: {ai_velocity:.2f} > 0.5)"
                    )
                    return {"decision": "HOLD", "confidence": 0}

        elif strat_type == "E":
            if side.upper() == "BUY" and ai_velocity < 0.0:
                print(
                    f"🚫 [Hard Lock] E BUY 기각: 추세 반전 미확인 (Velocity: {ai_velocity:.2f} < 0.0)"
                )
                return {"decision": "HOLD", "confidence": 0}

        elif strat_type == "F":
            if side.upper() == "SELL" and ai_velocity > 0.0:
                print(
                    f"🚫 [Hard Lock] F SELL 기각: 추세 반전 미확인 (Velocity: {ai_velocity:.2f} > 0.0)"
                )
                return {"decision": "HOLD", "confidence": 0}

    # 💡 페르소나 미션 분기
    if strat_type == "C":
        if step_num == 3:
            persona_mode = "RUTHLESS SCALPER (STRATEGY C - FINAL STEP)"
            mission = (
                "This is the FINAL DCA step. You must be EXTREMELY STRICT. "
                "Approve ONLY if there is a massive, confirmed momentum reversal. "
                "Target Hurdle: Score 75 or higher. Reject aggressively if uncertain."
            )
        else:
            persona_mode = "RUTHLESS SCALPER (STRATEGY C)"
            mission = (
                "You are a High-Frequency Scalper targeting highly volatile assets. "
                "TIME IS YOUR ENEMY. We do not hold positions. "
                "If there is NO immediate explosive momentum, reject the entry (Score below 60). "
                "Approve (Target Hurdle: 60) ONLY if the asset is strongly outperforming the market."
            )
    elif strat_type == "E":
        persona_mode = "LONG NOISE CATCHER (STRATEGY E)"
        mission = (
            "You are a Long-bias Swing Trader capturing oversold noise. "
            "TIME IS YOUR ALLY. We have a deep 5-step DCA capital pool. "
            "Differentiate between 'Temporary Oversold' (Approve) and 'Permanent Crash' (Deny). "
            "Be generous with early step entries (Target Hurdle: 60) if structural integrity holds."
        )
    elif strat_type == "F":
        persona_mode = "SHORT HEDGER (STRATEGY F)"
        mission = (
            "You are a Short-Selling Hedger in a downtrend. "
            "You act as a protective hedge for Strategy E. "
            "Calculate probability of continued negative velocity. "
            "Approve (Target Hurdle: 60) the SHORT entry if further decline is highly probable."
        )
    else:
        persona_mode = "GENERAL ANALYST"
        mission = "Provide an objective analysis based on the data."

    if curr_roi <= -0.05 and strat_type != "C":
        persona_mode = f"EMERGENCY RESCUE - {strat_type}"
        mission = (
            "EMERGENCY ALERT: CURRENT ROI IS BELOW -5%. ABANDON STRICT FILTERING. "
            "Your mission is to RESCUE the position by aggressively executing final DCA steps. "
            "Be GENEROUS and LENIENT with scoring (Target Hurdle: 60). "
            "If it's not a clear death spiral, approve entry to lower average price."
        )

    system_prompt = f"""
    You are a quantitative risk-management AI.
    Mode: {persona_mode}
    Your Mission: {mission}
    
    Given the Target Symbol, Direction (BUY or SELL), Current Price, DCA Step, Current ROI, Last 6 15m candles, 1m RSI, and AI Velocity:
    1. Evaluate the chart strictly based on your current Mode and Mission.
    2. Output ONLY a valid JSON object strictly in this format: 
    {{"decision": "EXECUTE", "confidence": 75}}
    Do not include markdown or other text.
    """

    recent_closes = [c[4] for c in ohlcv_15m[-6:]]
    recent_vols = [c[5] for c in ohlcv_15m[-6:]]

    prompt_data = (
        f"Target: {symbol} | Direction: {side.upper()} | Price: {current_price}\n"
        f"DCA Step: {step_num} | Current ROI: {curr_roi * 100:.2f}%\n"
        f"Last 6 15m Closes: {recent_closes}\n"
        f"Last 6 15m Vols: {recent_vols}\n"
        f"1m RSI: {rsi_1m:.2f} | AI Velocity: {ai_velocity:.2f}"
    )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    payload = {
        "max_tokens": 100,
        "system": system_prompt,
        "messages": [{"role": "user", "content": prompt_data}],
        "temperature": 0.0,
    }

    try:
        result = await asyncio.to_thread(
            _call_anthropic_sync, headers, payload, target_models=[PERSONA_MODEL]
        )

        if result and result.get("status") != "FATAL_ERROR" and "content" in result:
            raw_text = result["content"][0]["text"].strip()

            match_json = re.search(r"\{.*\}", raw_text, re.DOTALL)
            if match_json:
                parsed_data = json.loads(match_json.group(0))
                decision = parsed_data.get("decision", "HOLD").upper()
                confidence = int(parsed_data.get("confidence", 0))

                tag = "EMERGENCY" if curr_roi <= -0.05 else f"Step {step_num}"
                print(
                    f"⚡ [{strat_type} Persona - {tag}] {symbol} {side.upper()} 검증 -> {decision} (확신도: {confidence}%)"
                )
                return {"decision": decision, "confidence": confidence}

        return {"decision": "HOLD", "confidence": 0}
    except Exception as e:
        print(f"🚨 [{strat_type} Persona] 결재 에러: {e}")
        return {"decision": "HOLD", "confidence": 0}
