import asyncio
import copy
import html
import json
import re
import time
import xml.etree.ElementTree as ET

import requests

# ==========================================
# 1. 환경 설정 (15개 뉴스 소스)
# ==========================================
NEWS_SOURCES = {
    "policy": {
        "primary": [
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "https://bitcoinmagazine.com/.rss/full/",
            "https://www.theblock.co/rss.xml",
        ],
        "sub": ["https://coinjournal.net/news/category/regulation/feed/"],
    },
    "retail": {
        "primary": [
            "https://cointelegraph.com/rss",
            "https://decrypt.co/feed",
            "https://blockworks.co/feed",
        ],
        "sub": ["https://www.newsbtc.com/feed/"],
    },
    "data": {
        "primary": [
            "https://cryptopanic.com/news/rss/",
            "https://www.benzinga.com/markets/cryptocurrency/rss",
        ],
        "sub": ["https://www.investing.com/rss/news_25.rss"],
    },
}

# 💡 가중치 설정
CATEGORY_WEIGHTS = {"policy": 0.4, "retail": 0.3, "data": 0.3}

# 💡 캐시 및 데이터 유지 설정
CACHE_EXPIRE_SEC = 3600
LAST_SUCCESS_DATA = None
LAST_FETCH_TIME = 0

# ==========================================
# 💡 [V8.9.6] 알파 붕괴(Alpha Decay) 메모리 구축
# ==========================================
# 구조: {"cleaned_text": {"count": 1, "last_seen": timestamp}}
GLOBAL_SEEN_HEADLINES = {}
MEMORY_TTL_SEC = 43200  # 12시간 초과 뉴스 삭제 (메모리 누수 방지)


def _garbage_collect_memory(current_time):
    keys_to_delete = [
        k
        for k, v in GLOBAL_SEEN_HEADLINES.items()
        if current_time - v["last_seen"] > MEMORY_TTL_SEC
    ]
    for k in keys_to_delete:
        del GLOBAL_SEEN_HEADLINES[k]


# ==========================================
# 2. 데이터 정제 및 유사도 검증
# ==========================================
def clean_text(raw):
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub("<.*?>", "", text)
    return " ".join(text.split())


def is_similar(t1, t2):
    s1 = set(re.sub(r"[^\w\s]", "", t1.lower()).split())
    s2 = set(re.sub(r"[^\w\s]", "", t2.lower()).split())
    if not s1 or not s2:
        return False
    return len(s1 & s2) / len(s1 | s2) >= 0.5


# ==========================================
# 3. 비동기 하이브리드 엔진
# ==========================================
def _fetch_sync(url):
    try:
        resp = requests.get(url, timeout=5.0, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            return resp.text
    except:
        pass
    return None


async def process_category(cat_name, urls, current_time):
    headlines = []
    category_decay_list = []

    tasks = [asyncio.to_thread(_fetch_sync, u) for u in urls["primary"]]
    results = await asyncio.gather(*tasks)

    def parse_xml(xml_list, current_list):
        new_items = []
        for xml_data in xml_list:
            if not xml_data:
                continue
            try:
                root = ET.fromstring(xml_data)
                for item in root.findall(".//item")[:5]:
                    title = item.find("title").text if item.find("title") is not None else ""
                    clean = clean_text(title)
                    if not clean:
                        continue

                    is_duplicate = False
                    matched_key = None

                    # 글로벌 메모리와 유사도 교차 검증
                    for h in list(GLOBAL_SEEN_HEADLINES.keys()):
                        if is_similar(clean, h):
                            is_duplicate = True
                            matched_key = h
                            break

                    # 💡 [핵심] 재탕 뉴스는 삭제하지 않고 카운트 증가 후 감쇄율(Decay) 산출
                    if is_duplicate:
                        GLOBAL_SEEN_HEADLINES[matched_key]["count"] += 1
                        GLOBAL_SEEN_HEADLINES[matched_key]["last_seen"] = current_time

                        # 0.8의 지수 감쇄 (최하한선 0.2)
                        count = GLOBAL_SEEN_HEADLINES[matched_key]["count"]
                        decay = max(0.2, 0.8 ** (count - 1))

                        if matched_key not in current_list and matched_key not in new_items:
                            new_items.append(matched_key)
                            category_decay_list.append(decay)
                    else:
                        if not any(
                            is_similar(clean, existing) for existing in current_list + new_items
                        ):
                            GLOBAL_SEEN_HEADLINES[clean] = {"count": 1, "last_seen": current_time}
                            new_items.append(clean)
                            category_decay_list.append(1.0)
            except:
                pass
        return new_items

    headlines.extend(parse_xml(results, headlines))

    if len(headlines) < 2:
        sub_tasks = [asyncio.to_thread(_fetch_sync, u) for u in urls["sub"]]
        sub_results = await asyncio.gather(*sub_tasks)
        headlines.extend(parse_xml(sub_results, headlines))

    # 카테고리 내 뉴스들의 평균 감쇄율 산출
    avg_decay = sum(category_decay_list) / len(category_decay_list) if category_decay_list else 1.0

    return cat_name, {
        "text": " | ".join(headlines[:3]),
        "is_active": len(headlines) > 0,
        "weight": CATEGORY_WEIGHTS[cat_name],
        "decay_avg": avg_decay,
    }


# ==========================================
# 4. 메인 파이프라인
# ==========================================
async def fetch_ultra_news():
    global LAST_SUCCESS_DATA, LAST_FETCH_TIME
    current_time = time.time()

    # 메모리 누수 방지 가비지 컬렉터 실행
    _garbage_collect_memory(current_time)

    cat_tasks = [process_category(cat, urls, current_time) for cat, urls in NEWS_SOURCES.items()]
    cat_results = await asyncio.gather(*cat_tasks)

    result_payload = {
        "status": "success",
        "active_total_weight": 0.0,
        "system_decay_multiplier": 1.0,  # 💡 추가된 글로벌 감쇄 배수
        "integrity_warning": False,
        "is_cached": False,
        "categories": {},
    }

    total_decay = 0.0
    active_cats = 0

    for cat_name, cat_data in cat_results:
        result_payload["categories"][cat_name] = {
            "text": cat_data["text"],
            "is_active": cat_data["is_active"],
            "weight": cat_data["weight"],
        }
        if cat_data["is_active"]:
            result_payload["active_total_weight"] += cat_data["weight"]
            total_decay += cat_data["decay_avg"]
            active_cats += 1

    result_payload["active_total_weight"] = round(result_payload["active_total_weight"], 2)

    # 💡 전체 시스템 감쇄 배수 산출
    if active_cats > 0:
        result_payload["system_decay_multiplier"] = round(total_decay / active_cats, 3)

    if result_payload["active_total_weight"] >= 0.3:
        LAST_SUCCESS_DATA = copy.deepcopy(result_payload)
        LAST_FETCH_TIME = current_time
        return json.dumps(result_payload, ensure_ascii=False)

    if LAST_SUCCESS_DATA and (current_time - LAST_FETCH_TIME < CACHE_EXPIRE_SEC):
        cached_data = copy.deepcopy(LAST_SUCCESS_DATA)
        cached_data["is_cached"] = True
        # 캐시된 데이터는 오래되었으므로 강제 페널티 부여
        cached_data["system_decay_multiplier"] = 0.5
        return json.dumps(cached_data, ensure_ascii=False)

    fail_payload = {
        "status": "fail",
        "active_total_weight": 0.0,
        "system_decay_multiplier": 1.0,
        "integrity_warning": True,
        "categories": {
            "policy": {"text": "No data", "is_active": False, "weight": 0.4},
            "retail": {"text": "No data", "is_active": False, "weight": 0.3},
            "data": {"text": "No data", "is_active": False, "weight": 0.3},
        },
    }
    return json.dumps(fail_payload, ensure_ascii=False)


if __name__ == "__main__":
    start = time.time()
    res = asyncio.run(fetch_ultra_news())
    print(f"✅ 최종 엔진 가동 테스트 완료 (소요시간: {time.time() - start:.2f}초)")
    print(json.dumps(json.loads(res), indent=4, ensure_ascii=False))
