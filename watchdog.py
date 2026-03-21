import os
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime

# api.env 파일에서 텔레그램 정보 직접 파싱 (추가 라이브러리 의존성 제거)
BOT_TOKEN = ""
CHAT_ID = ""
try:
    with open("api.env", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("TELEGRAM_BOT_TOKEN"): 
                BOT_TOKEN = line.split("=")[1].strip().strip("'\"")
            elif line.strip().startswith("TELEGRAM_CHAT_ID"): 
                CHAT_ID = line.split("=")[1].strip().strip("'\"")
except Exception as e:
    print(f"⚠️ api.env 읽기 실패: {e}")

def send_alert(msg):
    if not BOT_TOKEN or not CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': msg}).encode('utf-8')
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=5)
    except: pass

def main():
    print("========================================")
    print("🛡️ [Watchdog] 시스템 무중단 관제 모듈 가동")
    print("========================================")
    
    HEARTBEAT_FILE = "heartbeat.txt"
    TIMEOUT_SECONDS = 180  # 3분 동안 응답 없으면 강제 재기동
    CHECK_INTERVAL = 30    # 30초 주기로 상태 검사

    while True:
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ▶ 메인 엔진(main.py) 프로세스 생성...")
        # 메인 스크립트를 하위 프로세스로 실행
        process = subprocess.Popen(["python", "main.py"])
        send_alert("🛡️ [Watchdog] 메인 엔진을 (재)가동합니다.")

        while True:
            time.sleep(CHECK_INTERVAL)

            # 1. 프로세스가 스스로 튕기거나 죽었는지 검사 (Crash)
            if process.poll() is not None:
                print(f"🚨 [Watchdog] 프로세스 비정상 종료 감지 (코드: {process.returncode}).")
                send_alert("🚨 [Watchdog] 메인 엔진 다운 감지. 10초 후 복구를 시도합니다.")
                break

            # 2. 프로세스는 살아있으나 코드가 멈췄는지 검사 (Hang/Deadlock)
            if os.path.exists(HEARTBEAT_FILE):
                last_mod = os.path.getmtime(HEARTBEAT_FILE)
                now = time.time()
                
                if now - last_mod > TIMEOUT_SECONDS:
                    print(f"🚨 [Watchdog] 하트비트 타임아웃! (마지막 갱신: {int(now - last_mod)}초 전)")
                    send_alert("🚨 [Watchdog] 메인 엔진 응답 없음(Heartbeat Timeout). 강제 킬(Kill) 및 재기동 수행.")
                    
                    # OS 단에서 무자비하게 프로세스 타격
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    break

        print("🔄 10초 대기 후 시스템을 다시 끌어올립니다...")
        time.sleep(10)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 관리자에 의해 Watchdog이 종료되었습니다.")