"""
Trinity Supervisor v2 — 프로세스 트리 관리 + deploy bot 통합
=============================================================
변경사항:
  - telegram_deploy_bot.py 를 PROGRAMS에 추가
  - 자식 프로세스 트리 kill (runner 등 하위 프로세스 포함)
  - 재시작 시 3→5초 대기 (프로세스 정리 시간 확보)
  - 중복 재시작 방지 (쿨다운 10초)
"""

import os
import sys
import time
import queue
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

# =========================================================
# UTF-8 출력
# =========================================================
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# =========================================================
# 기본 경로 / ENV
# =========================================================

# ★ v10.12: 크로스 플랫폼 경로
if os.name == "nt":
    BASE_DIR = Path(r"C:\Trinity").resolve()
else:
    BASE_DIR = Path.home() / "Rich-bot"
DEPLOY_ENV = BASE_DIR / "deploy_api.env"

if not DEPLOY_ENV.exists():
    print(f"[ERROR] deploy_api.env not found: {DEPLOY_ENV}")
    sys.exit(1)

load_dotenv(DEPLOY_ENV)

BOT_TOKEN = os.getenv("DEPLOY_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("DEPLOY_ALLOWED_CHAT_ID", "").strip()

if not BOT_TOKEN or not CHAT_ID:
    print("[ERROR] DEPLOY_BOT_TOKEN / DEPLOY_ALLOWED_CHAT_ID missing")
    sys.exit(1)

# =========================================================
# 실행 대상
# =========================================================

# ★ v10.12: sys.executable = systemd가 실행한 venv python 경로
_PYTHON = sys.executable

PROGRAMS = [
    {
        "name": "DEPLOY_BOT",
        "cmd": [_PYTHON, "telegram_deploy_bot.py"],
        "restart": True,
    },
    {
        "name": "WATCHDOG",
        "cmd": [_PYTHON, "watchdog.py"],
        "restart": True,
    },
    {
        "name": "TELEGRAM_BOT",
        "cmd": [_PYTHON, "telegram_bot.py"],
        "restart": True,
    },
]

# =========================================================
# 설정
# =========================================================

START_TS = time.time()
LAST_HEARTBEAT_TS = 0.0
RESTART_COOLDOWN_SEC = 10  # ★ 중복 재시작 방지 쿨다운

ERROR_KEYWORDS = [
    "traceback", "exception", "error", "fatal", "failed",
    "conflict", "terminated by other getupdates request",
    "runtimeerror", "valueerror", "typeerror", "keyerror",
    "attributeerror", "connectionerror", "timeout", "timed out",
    "refused", "cannot", "could not", "critical",
]

IGNORE_KEYWORDS = [
    "error_only",     # 로그 추출 키워드 오탐 방지
    "error_patterns",
    "[read_error]",
]

ERROR_DEDUP_SECONDS = 120
_recent_error_cache = {}
_cache_lock = threading.Lock()

log_queue = queue.Queue()
shutdown_flag = threading.Event()

# =========================================================
# 텔레그램 전송
# =========================================================

def send_telegram(text: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text[:3500]},
            timeout=10,
        )
    except Exception as e:
        print(f"[TG_SEND_FAIL] {e}", flush=True)

# =========================================================
# 유틸
# =========================================================

def now_str():
    # ★ V10.31AK: UTC 명시 — supervisor 로그 타임존 독립
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def print_log(msg: str):
    print(f"[{now_str()}] {msg}", flush=True)

def should_heartbeat() -> bool:
    global LAST_HEARTBEAT_TS
    now = time.time()
    elapsed = now - START_TS
    interval = 60 if elapsed <= 600 else 300
    if now - LAST_HEARTBEAT_TS >= interval:
        LAST_HEARTBEAT_TS = now
        return True
    return False

def normalize_line(line: str) -> str:
    return " ".join(line.strip().split()).lower()

def looks_like_error(line: str) -> bool:
    low = normalize_line(line)
    if not low:
        return False
    if any(k in low for k in IGNORE_KEYWORDS):
        return False
    return any(k in low for k in ERROR_KEYWORDS)

def should_send_error(line: str) -> bool:
    key = normalize_line(line)
    now = time.time()
    with _cache_lock:
        last = _recent_error_cache.get(key, 0)
        if now - last < ERROR_DEDUP_SECONDS:
            return False
        _recent_error_cache[key] = now
        old = [k for k, v in _recent_error_cache.items() if now - v > 1800]
        for k in old:
            _recent_error_cache.pop(k, None)
    return True

# =========================================================
# 프로세스 래퍼
# =========================================================

class ManagedProcess:
    def __init__(self, name: str, cmd: list, restart: bool):
        self.name = name
        self.cmd = cmd
        self.restart = restart
        self.proc = None
        self.stdout_thread = None
        self.stderr_thread = None
        self.last_restart_ts = 0.0  # ★ 쿨다운 추적

    def start(self):
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUTF8"] = "1"

        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(BASE_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="ignore",
            env=child_env,
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP
                           if os.name == "nt" else 0),
            **({"preexec_fn": os.setsid} if os.name != "nt" else {}),
        )

        print_log(f"{self.name} started | pid={self.proc.pid}")

        self.stdout_thread = threading.Thread(
            target=self._reader, args=(self.proc.stdout, "OUT"), daemon=True)
        self.stderr_thread = threading.Thread(
            target=self._reader, args=(self.proc.stderr, "ERR"), daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()
        self.last_restart_ts = time.time()

    def _reader(self, pipe, tag: str):
        try:
            for raw in iter(pipe.readline, ""):
                if shutdown_flag.is_set():
                    break
                line = raw.rstrip("\n\r")
                if line:
                    log_queue.put((self.name, tag, line))
        except Exception as e:
            log_queue.put((self.name, "SV", f"reader error: {e}"))

    def poll(self):
        return self.proc.poll() if self.proc else None

    def can_restart(self) -> bool:
        """쿨다운 내 중복 재시작 방지."""
        return time.time() - self.last_restart_ts > RESTART_COOLDOWN_SEC

    def stop(self):
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                print_log(f"Stopping {self.name} pid={self.proc.pid}")
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                else:
                    import signal as _sig
                    try:
                        os.killpg(os.getpgid(self.proc.pid), _sig.SIGTERM)
                    except ProcessLookupError:
                        pass
                    except Exception:
                        self.proc.terminate()
        except Exception as e:
            print_log(f"Stop failed {self.name}: {e}")

# =========================================================
# 로그 소비
# =========================================================

def log_consumer():
    while not shutdown_flag.is_set():
        try:
            name, source, line = log_queue.get(timeout=1)
        except queue.Empty:
            continue

        print_log(f"[{name}][{source}] {line}")

        if looks_like_error(line) and should_send_error(line):
            send_telegram(f"⚠️ {name} [{source}]\n{line}")

# =========================================================
# ★ 시작 전 기존 프로세스 정리
# =========================================================

def kill_existing_instances():
    """supervisor 시작 전, 이전 실행의 잔여 프로세스를 모두 kill."""
    import signal as _signal
    my_pid = os.getpid()
    killed = []
    targets = [cfg["cmd"][-1].lower() for cfg in PROGRAMS]
    targets.append("trinity_supervisor.py")

    if os.name == "nt":
        import csv as _csv
        import io as _io
        try:
            out = subprocess.check_output(
                ["wmic", "process", "get",
                 "ProcessId,Name,CommandLine", "/FORMAT:CSV"],
                text=True, stderr=subprocess.DEVNULL,
                encoding="utf-8", errors="ignore",
            )
            rows = list(_csv.DictReader(_io.StringIO(out)))
        except Exception as e:
            print_log(f"[CLEANUP] wmic 실패: {e}")
            return killed
        for row in rows:
            try:
                pid = int((row.get("ProcessId") or "").strip())
            except Exception:
                continue
            if pid == my_pid:
                continue
            cmdline = (row.get("CommandLine") or "").lower()
            name = (row.get("Name") or "").lower()
            if name not in ("python.exe", "pythonw.exe"):
                continue
            for target in targets:
                if target in cmdline:
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                        killed.append((pid, target))
                        print_log(f"[CLEANUP] killed pid={pid} ({target})")
                    except Exception:
                        pass
                    break
    else:
        # ★ Linux: /proc 기반
        for proc_dir in Path("/proc").iterdir():
            if not proc_dir.name.isdigit():
                continue
            pid = int(proc_dir.name)
            if pid == my_pid:
                continue
            try:
                cmdline = (proc_dir / "cmdline").read_text().replace("\x00", " ").lower()
            except Exception:
                continue
            if "python" not in cmdline:
                continue
            for target in targets:
                if target in cmdline:
                    try:
                        os.kill(pid, _signal.SIGTERM)
                        killed.append((pid, target))
                        print_log(f"[CLEANUP] killed pid={pid} ({target})")
                    except Exception:
                        pass
                    break
    return killed


# =========================================================
# 메인
# =========================================================

def main():
    print_log("TRINITY SUPERVISOR v2 START")

    # ★ 기존 잔여 프로세스 전부 정리
    cleaned = kill_existing_instances()
    if cleaned:
        print_log(f"[CLEANUP] {len(cleaned)}개 기존 프로세스 정리 완료")
        time.sleep(3)  # 정리 후 안정화 대기

    send_telegram("🟢 TRINITY SUPERVISOR v2 시작")

    processes = []
    for cfg in PROGRAMS:
        mp = ManagedProcess(cfg["name"], cfg["cmd"], cfg["restart"])
        processes.append(mp)

    for mp in processes:
        try:
            mp.start()
            time.sleep(2)  # ★ 시작 간격 2초
        except Exception as e:
            print_log(f"Start failed {mp.name}: {e}")
            send_telegram(f"🔴 시작 실패: {mp.name}\n{e}")

    consumer = threading.Thread(target=log_consumer, daemon=True)
    consumer.start()

# =========================================================
# ★ 신호 파일 / PID 파일
# =========================================================

RESTART_SIGNAL_FILE = BASE_DIR / "_restart_signal"
SUPERVISOR_PIDS_FILE = BASE_DIR / "_supervisor_pids.json"


def write_pids_file(processes: list):
    """deploy bot이 읽을 수 있도록 PID 상태를 JSON으로 기록."""
    import json as _json
    data = {
        "updated_at": now_str(),
        "supervisor_pid": os.getpid(),
        "processes": {},
    }
    for mp in processes:
        rc = mp.poll()
        data["processes"][mp.name] = {
            "pid": mp.proc.pid if mp.proc else None,
            "state": "RUN" if rc is None else f"EXIT({rc})",
        }
    try:
        with open(SUPERVISOR_PIDS_FILE, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def check_restart_signal(processes: list):
    """
    deploy bot이 생성한 _restart_signal 파일이 있으면:
    1. DEPLOY_BOT 제외 프로세스 전부 kill + 재시작
    2. 신호 파일 삭제
    """
    if not RESTART_SIGNAL_FILE.exists():
        return

    try:
        import json as _json
        with open(RESTART_SIGNAL_FILE, "r", encoding="utf-8") as f:
            signal_data = _json.load(f)
        reason = signal_data.get("reason", "unknown")
    except Exception:
        reason = "unknown"

    print_log(f"[RESTART_SIGNAL] 감지: {reason}")
    send_telegram(f"🔄 재시작 신호 감지: {reason}")

    # 신호 파일 즉시 삭제 (중복 처리 방지)
    try:
        RESTART_SIGNAL_FILE.unlink()
    except Exception:
        pass

    # DEPLOY_BOT 제외하고 나머지 kill + 재시작
    for mp in processes:
        if mp.name == "DEPLOY_BOT":
            continue  # deploy bot은 살려둠

        # kill
        mp.stop()
        print_log(f"[RESTART_SIGNAL] {mp.name} 종료")

    time.sleep(3)  # 프로세스 정리 대기

    for mp in processes:
        if mp.name == "DEPLOY_BOT":
            continue

        rc = mp.poll()
        if rc is not None:  # 종료 확인됨
            try:
                mp.start()
                print_log(f"[RESTART_SIGNAL] {mp.name} 재시작 pid={mp.proc.pid}")
            except Exception as e:
                print_log(f"[RESTART_SIGNAL] {mp.name} 재시작 실패: {e}")

    send_telegram(f"✅ 재시작 완료 ({reason})")


# =========================================================
# 메인
# =========================================================

def main():
    print_log("TRINITY SUPERVISOR v2 START")

    # ★ 기존 잔여 프로세스 전부 정리
    cleaned = kill_existing_instances()
    if cleaned:
        print_log(f"[CLEANUP] {len(cleaned)}개 기존 프로세스 정리 완료")
        time.sleep(3)

    # ★ 이전 신호 파일 정리
    if RESTART_SIGNAL_FILE.exists():
        RESTART_SIGNAL_FILE.unlink(missing_ok=True)

    send_telegram("🟢 TRINITY SUPERVISOR v2 시작")

    processes = []
    for cfg in PROGRAMS:
        mp = ManagedProcess(cfg["name"], cfg["cmd"], cfg["restart"])
        processes.append(mp)

    for mp in processes:
        try:
            mp.start()
            time.sleep(2)
        except Exception as e:
            print_log(f"Start failed {mp.name}: {e}")
            send_telegram(f"🔴 시작 실패: {mp.name}\n{e}")

    consumer = threading.Thread(target=log_consumer, daemon=True)
    consumer.start()

    try:
        while True:
            # ★ 재시작 신호 체크 (매 루프)
            check_restart_signal(processes)

            if should_heartbeat():
                alive = []
                for mp in processes:
                    pid = mp.proc.pid if mp.proc else None
                    rc = mp.poll()
                    s = "RUN" if rc is None else f"EXIT({rc})"
                    alive.append(f"{mp.name}:{s}:pid={pid}")
                print_log(f"[HB] {' | '.join(alive)}")

                # ★ PID 파일 갱신 (heartbeat마다)
                write_pids_file(processes)

            for mp in processes:
                rc = mp.poll()
                if rc is not None and mp.restart and not shutdown_flag.is_set():
                    if not mp.can_restart():
                        continue

                    msg = f"🔴 {mp.name} 종료(code={rc}) → 재시작"
                    print_log(msg)
                    send_telegram(msg)

                    time.sleep(5)
                    try:
                        mp.start()
                        send_telegram(f"🟡 {mp.name} 재시작 완료 | pid={mp.proc.pid}")
                    except Exception as e:
                        err = f"🔴 {mp.name} 재시작 실패: {e}"
                        print_log(err)
                        send_telegram(err)

            time.sleep(1)

    except KeyboardInterrupt:
        print_log("KeyboardInterrupt")
    finally:
        shutdown_flag.set()
        for mp in processes:
            mp.stop()
        send_telegram("🛑 TRINITY SUPERVISOR v2 종료")


if __name__ == "__main__":
    main()
