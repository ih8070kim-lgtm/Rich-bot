"""
Trinity Deploy Bot v2 — 텔레그램 인라인 버튼 + v9_logs 지원
=============================================================
변경사항 (v1 → v2):
  - 인라인 키보드: 모든 명령을 버튼 클릭으로 실행
  - v9_logs 경로 로그 추출 (200줄/500줄/에러만)
  - 프로세스 트리 kill (자식 프로세스 포함)
  - callback_query 처리 + allowed_updates 수정
  - /menu 로 언제든 메인 메뉴 호출
  - 배포 후 자동 프로세스 트리 재시작
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# =========================================================
# 기본 경로 / ENV
# =========================================================

PROJECT_DIR = Path(r"C:\Trinity").resolve()
ENV_PATH = PROJECT_DIR / "deploy_api.env"

if not ENV_PATH.exists():
    print(f"[ERROR] deploy_api.env not found: {ENV_PATH}")
    sys.exit(1)

load_dotenv(ENV_PATH)

BOT_TOKEN = os.getenv("DEPLOY_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID_RAW = os.getenv("DEPLOY_ALLOWED_CHAT_ID", "").strip()

if not BOT_TOKEN:
    print("[ERROR] DEPLOY_BOT_TOKEN missing"); sys.exit(1)
if not ALLOWED_CHAT_ID_RAW:
    print("[ERROR] DEPLOY_ALLOWED_CHAT_ID missing"); sys.exit(1)

try:
    ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID_RAW)
except ValueError:
    print("[ERROR] DEPLOY_ALLOWED_CHAT_ID must be int"); sys.exit(1)

# =========================================================
# 경로 설정
# =========================================================

BASE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

V9_LOG_DIR       = PROJECT_DIR / "v9_logs"          # ★ 실제 봇 로그 경로
DEPLOY_LOG_DIR   = PROJECT_DIR / "_deploy_logs"     # deploy 자체 로그 (레거시)

TG_INBOX_DIR     = PROJECT_DIR / "_tg_inbox"
TG_DONE_DIR      = PROJECT_DIR / "_tg_done"
TG_FAILED_DIR    = PROJECT_DIR / "_tg_failed"
TG_TMP_DIR       = PROJECT_DIR / "_tg_tmp"
DEPLOY_BACKUP_ROOT = PROJECT_DIR / "_deploy_backups"
DEPLOY_REPORT_DIR  = PROJECT_DIR / "_deploy_reports"
OFFSET_FILE      = PROJECT_DIR / "_telegram_deploy_offset.json"

# =========================================================
# 프로세스 관리 설정
# =========================================================

USE_SUPERVISOR_MODE = True

# (프로세스 관리는 supervisor가 담당 — deploy bot은 신호 파일만 생성)

# =========================================================
# 배포 설정
# =========================================================

POLL_TIMEOUT = 30
ALLOW_MULTI_MATCH = False

EXCLUDE_DIRS = {
    ".git", "__pycache__", ".venv", "venv",
    "_tg_inbox", "_tg_done", "_tg_failed", "_tg_tmp",
    "_deploy_backups", "_deploy_logs", "_deploy_reports",
    "v9_logs", "node_modules",
}

DEPLOY_BLOCKED_FILENAMES = {
    "api.env", "deploy_api.env", "telegram_deploy_bot.py",
}

DOWNLOAD_BLOCKED_FILENAMES = {
    "api.env", "deploy_api.env",
}

ALLOWED_DEPLOY_SUFFIXES = {".py"}
ALLOWED_DOWNLOAD_SUFFIXES = {".py", ".log", ".json", ".txt", ".md", ".csv", ".env"}

# =========================================================
# 유틸
# =========================================================

def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def log(msg: str) -> None:
    print(f"[{now_str()}] {msg}", flush=True)

# =========================================================
# 텔레그램 API
# =========================================================

def tg_api(method: str, payload=None, files=None, timeout=60):
    url = f"{BASE_URL}/{method}"
    if files:
        r = requests.post(url, data=payload, files=files, timeout=timeout)
    else:
        r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data["result"]


def send_msg(chat_id: int, text: str, buttons=None, parse_mode=None):
    """텍스트 + 선택적 인라인 키보드 전송."""
    payload = {"chat_id": chat_id, "text": text[:4000]}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    tg_api("sendMessage", payload)


def edit_msg(chat_id: int, message_id: int, text: str, buttons=None):
    """기존 메시지를 수정 (버튼 교체)."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4000],
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": buttons}
    try:
        tg_api("editMessageText", payload)
    except Exception:
        # 메시지 수정 실패 시 새 메시지 전송
        send_msg(chat_id, text, buttons)


def answer_callback(callback_query_id: str, text: str = ""):
    """콜백 쿼리 응답 (버튼 누름 확인)."""
    try:
        tg_api("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text[:200] if text else "",
        })
    except Exception:
        pass


def send_document(chat_id: int, file_path: Path, caption: Optional[str] = None):
    if not file_path.exists() or not file_path.is_file():
        send_msg(chat_id, f"파일 없음: {file_path}")
        return
    with open(file_path, "rb") as f:
        tg_api(
            "sendDocument",
            payload={"chat_id": str(chat_id), "caption": (caption or file_path.name)[:200]},
            files={"document": (file_path.name, f)},
            timeout=120,
        )


def get_updates(offset: Optional[int]):
    payload = {
        "timeout": POLL_TIMEOUT,
        "allowed_updates": ["message", "callback_query"],  # ★ callback_query 추가
    }
    if offset is not None:
        payload["offset"] = offset
    return tg_api("getUpdates", payload, timeout=POLL_TIMEOUT + 10)


def get_file_path(file_id: str) -> str:
    return tg_api("getFile", {"file_id": file_id})["file_path"]


def download_telegram_file(file_path: str, save_to: Path):
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    save_to.parent.mkdir(parents=True, exist_ok=True)
    with open(save_to, "wb") as f:
        f.write(r.content)

# =========================================================
# 인라인 키보드 빌더
# =========================================================

def btn(text: str, data: str) -> dict:
    """인라인 버튼 1개."""
    return {"text": text, "callback_data": data}


def main_menu_buttons() -> list:
    return [
        [btn("📊 상태", "status"), btn("🔄 재시작", "restart")],
        [btn("📋 로그 200줄", "log_200"), btn("📋 로그 500줄", "log_500")],
        [btn("🚨 에러 로그", "log_err"), btn("📂 로그 파일 목록", "log_list")],
        [btn("📁 파일 받기", "get_menu"), btn("💾 백업 목록", "backup_list")],
    ]


def log_file_buttons(files: list, prefix: str = "logfile") -> list:
    """로그 파일 목록을 2열 버튼으로."""
    rows = []
    for i in range(0, len(files), 2):
        row = [btn(f"📄 {files[i].name}", f"{prefix}:{files[i].name}")]
        if i + 1 < len(files):
            row.append(btn(f"📄 {files[i+1].name}", f"{prefix}:{files[i+1].name}"))
        rows.append(row)
    rows.append([btn("⬅️ 메뉴", "menu")])
    return rows


def confirm_restart_buttons() -> list:
    return [
        [btn("✅ 확인 — 재시작", "restart_confirm")],
        [btn("❌ 취소", "menu")],
    ]

# =========================================================
# 디렉터리 / 파일 유틸
# =========================================================

def ensure_dirs():
    for p in [TG_INBOX_DIR, TG_DONE_DIR, TG_FAILED_DIR, TG_TMP_DIR,
              DEPLOY_BACKUP_ROOT, DEPLOY_LOG_DIR, DEPLOY_REPORT_DIR]:
        p.mkdir(parents=True, exist_ok=True)


def load_offset() -> Optional[int]:
    if not OFFSET_FILE.exists():
        return None
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("offset")
    except Exception:
        return None


def save_offset(offset: int):
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": offset}, f)


def move_file(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = dst_dir / f"{ts}__{src.name}"
    shutil.move(str(src), str(dst))
    return dst


def create_backup_root() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = DEPLOY_BACKUP_ROOT / ts
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_report(report: dict) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = DEPLOY_REPORT_DIR / f"deploy_report_{ts}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def backup_file(target: Path, backup_root: Path) -> Path:
    rel = target.relative_to(PROJECT_DIR)
    dst = backup_root / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dst)
    return dst


def atomic_replace(src: Path, dst: Path):
    tmp = dst.with_suffix(dst.suffix + ".deploy_tmp")
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)

# =========================================================
# 파일 탐색
# =========================================================

def list_project_files_by_name(filename: str) -> list:
    matches = []
    for p in PROJECT_DIR.rglob("*"):
        if not p.is_file():
            continue
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        if p.name == filename:
            matches.append(p)
    return matches


def is_safe_download(path: Path) -> bool:
    if path.name.lower() in DOWNLOAD_BLOCKED_FILENAMES:
        return False
    if path.suffix.lower() not in ALLOWED_DOWNLOAD_SUFFIXES:
        return False
    return True


def find_single_project_file(filename: str):
    matches = list_project_files_by_name(filename)
    if not matches:
        return False, f"파일 못 찾음: {filename}", None
    if len(matches) > 1 and not ALLOW_MULTI_MATCH:
        preview = "\n".join(str(x) for x in matches[:5])
        return False, f"동명 파일 복수:\n{preview}", None
    target = matches[0]
    if not is_safe_download(target):
        return False, f"다운로드 차단: {filename}", None
    return True, "ok", target


def get_latest_backup_file(filename: str):
    if filename.lower() in DOWNLOAD_BLOCKED_FILENAMES:
        return False, "차단 파일", None
    if not DEPLOY_BACKUP_ROOT.exists():
        return False, "백업 폴더 없음", None
    dirs = sorted([p for p in DEPLOY_BACKUP_ROOT.iterdir() if p.is_dir()],
                  key=lambda x: x.name, reverse=True)
    for d in dirs:
        m = list(d.rglob(filename))
        if len(m) == 1:
            return True, "ok", m[0]
    return False, f"백업에서 못 찾음: {filename}", None

# =========================================================
# ★ v9_logs 로그 추출 (핵심 신기능)
# =========================================================

def get_v9_log_files() -> list:
    """v9_logs 디렉터리의 로그 파일 목록 (최근 수정순)."""
    if not V9_LOG_DIR.exists():
        return []
    files = [f for f in V9_LOG_DIR.iterdir()
             if f.is_file() and f.suffix in (".log", ".csv", ".txt")]
    return sorted(files, key=lambda x: x.stat().st_mtime, reverse=True)


def tail_lines(path: Path, n: int = 200) -> list:
    """파일 끝에서 n줄 읽기."""
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return list(deque(f, maxlen=n))
    except Exception as e:
        return [f"[READ_ERROR] {e}\n"]


def build_log_extract(line_count: int = 200, error_only: bool = False) -> tuple:
    """
    v9_logs의 모든 로그 파일에서 마지막 N줄 추출.
    error_only=True: ERROR/EXCEPTION/Traceback 포함 줄만.
    반환: (ok, msg, file_path)
    """
    files = get_v9_log_files()
    if not files:
        # fallback: _deploy_logs도 확인
        if DEPLOY_LOG_DIR.exists():
            files = sorted(DEPLOY_LOG_DIR.glob("*.log"),
                           key=lambda x: x.stat().st_mtime, reverse=True)
        if not files:
            return False, "로그 파일 없음 (v9_logs 비어있음)", None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "errors" if error_only else f"tail{line_count}"
    out_path = TG_TMP_DIR / f"log_{mode}_{ts}.txt"
    TG_TMP_DIR.mkdir(parents=True, exist_ok=True)

    error_patterns = re.compile(
        r"(error|exception|traceback|fatal|failed|critical|crash)",
        re.IGNORECASE
    )

    total_lines = 0
    try:
        with open(out_path, "w", encoding="utf-8", errors="ignore") as out:
            out.write(f"[LOG EXTRACT] {now_str()}\n")
            out.write(f"mode: {mode} | source: {V9_LOG_DIR}\n")
            out.write(f"files: {len(files)}\n")
            out.write("=" * 80 + "\n\n")

            for lf in files:
                lines = tail_lines(lf, line_count)
                if error_only:
                    # 에러 줄 + 앞뒤 컨텍스트 2줄
                    filtered = []
                    for i, line in enumerate(lines):
                        if error_patterns.search(line):
                            start = max(0, i - 2)
                            end = min(len(lines), i + 3)
                            for j in range(start, end):
                                if lines[j] not in filtered:
                                    filtered.append(lines[j])
                    lines = filtered

                if not lines:
                    continue

                out.write(f"[FILE] {lf.name}  ({lf.stat().st_size / 1024:.0f}KB)\n")
                out.write("-" * 80 + "\n")
                for line in lines:
                    out.write(line.rstrip("\n") + "\n")
                    total_lines += 1
                out.write("\n" + "=" * 80 + "\n\n")

        if total_lines == 0:
            out_path.unlink(missing_ok=True)
            return False, "해당 조건 로그 없음", None

        return True, f"{total_lines}줄 추출", out_path

    except Exception as e:
        return False, f"로그 추출 실패: {e}", None


def send_single_log_file(chat_id: int, filename: str, line_count: int = 500):
    """개별 로그 파일의 tail N줄을 전송."""
    path = V9_LOG_DIR / filename
    if not path.exists():
        path = DEPLOY_LOG_DIR / filename
    if not path.exists():
        send_msg(chat_id, f"파일 없음: {filename}")
        return

    lines = tail_lines(path, line_count)
    if not lines:
        send_msg(chat_id, f"빈 파일: {filename}")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = TG_TMP_DIR / f"{filename}_{ts}.txt"
    TG_TMP_DIR.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", errors="ignore") as f:
        f.writelines(lines)

    send_document(chat_id, out, caption=f"{filename} (최근 {len(lines)}줄)")

# =========================================================
# ★ 프로세스 관리 — supervisor 신호 파일 방식
# =========================================================
# wmic는 한글 경로에서 불안정 → supervisor에게 파일로 신호
#
# 구조:
#   _restart_signal   → deploy bot이 생성, supervisor가 읽고 삭제+재시작
#   _supervisor_pids.json → supervisor가 매 heartbeat마다 갱신
# =========================================================

RESTART_SIGNAL_FILE = PROJECT_DIR / "_restart_signal"
SUPERVISOR_PIDS_FILE = PROJECT_DIR / "_supervisor_pids.json"


def request_restart(reason: str = "deploy") -> bool:
    """supervisor에게 재시작 신호 전송 (파일 생성)."""
    try:
        with open(RESTART_SIGNAL_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "reason": reason,
                "requested_at": datetime.now().isoformat(),
                "requested_by_pid": os.getpid(),
            }, f)
        log(f"Restart signal created: {reason}")
        return True
    except Exception as e:
        log(f"Restart signal failed: {e}")
        return False


def get_runtime_status() -> str:
    """supervisor가 기록한 PID 파일에서 상태 읽기."""
    if not SUPERVISOR_PIDS_FILE.exists():
        return "  supervisor PID 파일 없음 (supervisor 미실행?)"

    try:
        with open(SUPERVISOR_PIDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "  PID 파일 읽기 실패"

    lines = []
    updated = data.get("updated_at", "?")
    lines.append(f"  (갱신: {updated})")

    for name, info in data.get("processes", {}).items():
        pid = info.get("pid", "?")
        state = info.get("state", "?")
        emoji = "✅" if state == "RUN" else "❌"
        lines.append(f"  {emoji} {name}: pid={pid} ({state})")

    return "\n".join(lines)

# =========================================================
# 배포 로직
# =========================================================

def deploy_file(incoming: Path) -> tuple:
    filename = incoming.name

    if filename.lower() in DEPLOY_BLOCKED_FILENAMES:
        return False, f"차단 파일: {filename}"
    if incoming.suffix.lower() not in ALLOWED_DEPLOY_SUFFIXES:
        return False, f".py만 배포 가능"

    matches = list_project_files_by_name(filename)
    if not matches:
        return False, f"대상 파일 못 찾음: {filename}"
    if len(matches) > 1 and not ALLOW_MULTI_MATCH:
        return False, f"동명 파일 복수: {filename}"

    target = matches[0]
    backup_root = create_backup_root()
    backup_path = backup_file(target, backup_root)

    # ★ 파일 교체
    atomic_replace(incoming, target)

    # ★ supervisor에게 재시작 신호 전송
    restart_ok = request_restart(f"deploy:{filename}")

    report = create_report({
        "file": filename,
        "target": str(target),
        "backup": str(backup_path),
        "restart_signaled": restart_ok,
        "timestamp": datetime.now().isoformat(),
    })

    return True, (
        f"✅ 배포 완료\n"
        f"📄 {filename}\n"
        f"📍 {target.relative_to(PROJECT_DIR)}\n"
        f"💾 백업: {backup_path.name}\n"
        f"🔄 재시작 신호: {'전송됨' if restart_ok else '실패!'}\n"
        f"⏳ supervisor가 5초 내 재시작"
    )

# =========================================================
# ★ 콜백 처리 (버튼 클릭)
# =========================================================

def handle_callback(chat_id: int, message_id: int, cb_id: str, data: str):
    """인라인 버튼 콜백 처리."""
    answer_callback(cb_id)

    # ── 메인 메뉴
    if data == "menu":
        edit_msg(chat_id, message_id,
                 "🤖 Trinity Deploy Bot\n원하는 기능을 선택하세요:",
                 main_menu_buttons())
        return

    # ── 상태
    if data == "status":
        rt = get_runtime_status()
        text = (
            f"📊 상태 ({now_str()})\n\n"
            f"📂 {PROJECT_DIR}\n"
            f"🤖 deploy bot: pid {os.getpid()}\n"
            f"👤 parent: pid {os.getppid()}\n\n"
            f"실행 중 프로세스:\n{rt}"
        )
        edit_msg(chat_id, message_id, text,
                 [[btn("🔄 새로고침", "status"), btn("⬅️ 메뉴", "menu")]])
        return

    # ── 재시작 (확인 단계)
    if data == "restart":
        edit_msg(chat_id, message_id,
                 "⚠️ watchdog + telegram_bot 프로세스 트리를 종료합니다.\n"
                 "supervisor가 자동 재시작합니다.\n\n정말 실행?",
                 confirm_restart_buttons())
        return

    if data == "restart_confirm":
        ok = request_restart("manual_restart")
        edit_msg(chat_id, message_id,
                 f"🔄 재시작 신호 {'전송됨' if ok else '실패!'}\nsupervisor가 5초 내 재시작",
                 [[btn("📊 상태 확인", "status"), btn("⬅️ 메뉴", "menu")]])
        return

    # ── 로그 200줄
    if data == "log_200":
        edit_msg(chat_id, message_id, "📋 로그 추출 중 (200줄)...", [])
        ok, msg, path = build_log_extract(200)
        if ok and path:
            send_document(chat_id, path, caption=f"v9_logs 최근 200줄 ({msg})")
        else:
            send_msg(chat_id, f"❌ {msg}")
        send_msg(chat_id, "완료", [[btn("⬅️ 메뉴", "menu")]])
        return

    # ── 로그 500줄
    if data == "log_500":
        edit_msg(chat_id, message_id, "📋 로그 추출 중 (500줄)...", [])
        ok, msg, path = build_log_extract(500)
        if ok and path:
            send_document(chat_id, path, caption=f"v9_logs 최근 500줄 ({msg})")
        else:
            send_msg(chat_id, f"❌ {msg}")
        send_msg(chat_id, "완료", [[btn("⬅️ 메뉴", "menu")]])
        return

    # ── 에러 로그
    if data == "log_err":
        edit_msg(chat_id, message_id, "🚨 에러 로그 추출 중...", [])
        ok, msg, path = build_log_extract(1000, error_only=True)
        if ok and path:
            send_document(chat_id, path, caption=f"에러 로그 ({msg})")
        else:
            send_msg(chat_id, f"❌ {msg}")
        send_msg(chat_id, "완료", [[btn("⬅️ 메뉴", "menu")]])
        return

    # ── 로그 파일 목록
    if data == "log_list":
        files = get_v9_log_files()
        if not files:
            edit_msg(chat_id, message_id, "v9_logs 비어있음",
                     [[btn("⬅️ 메뉴", "menu")]])
            return
        text = f"📂 v9_logs ({len(files)}개)\n파일 클릭 → 최근 500줄 전송"
        edit_msg(chat_id, message_id, text, log_file_buttons(files[:20]))
        return

    # ── 개별 로그 파일 전송
    if data.startswith("logfile:"):
        filename = data.split(":", 1)[1]
        send_msg(chat_id, f"📄 {filename} 전송 중...")
        send_single_log_file(chat_id, filename, 500)
        send_msg(chat_id, "완료", [[btn("📂 목록", "log_list"), btn("⬅️ 메뉴", "menu")]])
        return

    # ── 파일 받기 안내
    if data == "get_menu":
        edit_msg(chat_id, message_id,
                 "📁 파일 받기\n\n"
                 "채팅에 파일명을 입력하세요:\n"
                 "예) planners.py\n\n"
                 "또는 자주 쓰는 파일:",
                 [
                     [btn("planners.py", "getfile:planners.py"),
                      btn("config.py", "getfile:config.py")],
                     [btn("strategy_core.py", "getfile:strategy_core.py"),
                      btn("hedge_engine_v2.py", "getfile:hedge_engine_v2.py")],
                     [btn("runner.py", "getfile:runner.py"),
                      btn("risk_manager.py", "getfile:risk_manager.py")],
                     [btn("⬅️ 메뉴", "menu")],
                 ])
        return

    # ── 파일 전송
    if data.startswith("getfile:"):
        filename = data.split(":", 1)[1]
        ok, msg, path = find_single_project_file(filename)
        if ok and path:
            send_document(chat_id, path, caption=filename)
        else:
            send_msg(chat_id, f"❌ {msg}")
        send_msg(chat_id, "완료", [[btn("📁 파일", "get_menu"), btn("⬅️ 메뉴", "menu")]])
        return

    # ── 백업 목록
    if data == "backup_list":
        if not DEPLOY_BACKUP_ROOT.exists():
            edit_msg(chat_id, message_id, "백업 폴더 없음",
                     [[btn("⬅️ 메뉴", "menu")]])
            return
        dirs = sorted([d for d in DEPLOY_BACKUP_ROOT.iterdir() if d.is_dir()],
                      key=lambda x: x.name, reverse=True)[:10]
        if not dirs:
            edit_msg(chat_id, message_id, "백업 없음",
                     [[btn("⬅️ 메뉴", "menu")]])
            return
        text = "💾 최근 백업:\n" + "\n".join(f"  📁 {d.name}" for d in dirs)
        text += "\n\n채팅에 파일명 입력: 예) planners.py"
        edit_msg(chat_id, message_id, text,
                 [[btn("⬅️ 메뉴", "menu")]])
        return

    # fallback
    send_msg(chat_id, "알 수 없는 명령",
             [[btn("⬅️ 메뉴", "menu")]])

# =========================================================
# 텍스트 명령 처리
# =========================================================

def handle_text(chat_id: int, text: str):
    raw = text.strip()
    cmd = raw.lower()

    # 메뉴 명령
    if cmd in ("/start", "/help", "/menu", "메뉴"):
        send_msg(chat_id,
                 "🤖 Trinity Deploy Bot v2\n\n"
                 "버튼으로 조작하거나 명령어 입력:\n"
                 "  /menu — 메인 메뉴\n"
                 "  /get 파일명\n"
                 "  /log 200|500|err\n"
                 "  /restart\n\n"
                 "📎 .py 파일 전송 → 자동 배포+재시작",
                 main_menu_buttons())
        return

    if cmd == "/status":
        rt = get_runtime_status()
        send_msg(chat_id,
                 f"📊 상태 ({now_str()})\n\n"
                 f"📂 {PROJECT_DIR}\n"
                 f"🤖 pid {os.getpid()} | parent {os.getppid()}\n\n"
                 f"프로세스:\n{rt}",
                 [[btn("🔄 새로고침", "status"), btn("⬅️ 메뉴", "menu")]])
        return

    if cmd == "/restart":
        send_msg(chat_id,
                 "⚠️ 프로세스 트리 종료 + supervisor 재시작?",
                 confirm_restart_buttons())
        return

    # /log 명령 (단축)
    if cmd.startswith("/log"):
        parts = raw.split()
        if len(parts) == 1:
            # /log만 → 200줄 기본
            send_msg(chat_id, "📋 추출 중...")
            ok, msg, path = build_log_extract(200)
            if ok and path:
                send_document(chat_id, path, caption=f"v9_logs 200줄 ({msg})")
            else:
                send_msg(chat_id, f"❌ {msg}")
            return

        arg = parts[1].lower()
        if arg == "err":
            send_msg(chat_id, "🚨 에러 추출 중...")
            ok, msg, path = build_log_extract(1000, error_only=True)
            if ok and path:
                send_document(chat_id, path, caption=f"에러 ({msg})")
            else:
                send_msg(chat_id, f"❌ {msg}")
            return

        try:
            n = int(arg)
            n = max(10, min(2000, n))
        except ValueError:
            send_msg(chat_id, "사용법: /log 200 | /log 500 | /log err")
            return

        send_msg(chat_id, f"📋 {n}줄 추출 중...")
        ok, msg, path = build_log_extract(n)
        if ok and path:
            send_document(chat_id, path, caption=f"v9_logs {n}줄 ({msg})")
        else:
            send_msg(chat_id, f"❌ {msg}")
        return

    # /get 파일명 (레거시 호환)
    if raw.lower().startswith("/get "):
        filename = raw.split(" ", 1)[1].strip()
        if not filename:
            send_msg(chat_id, "사용법: /get planners.py")
            return
        ok, msg, path = find_single_project_file(filename)
        if ok and path:
            send_document(chat_id, path, caption=filename)
        else:
            send_msg(chat_id, f"❌ {msg}")
        return

    # /backup 파일명
    if raw.lower().startswith("/backup "):
        filename = raw.split(" ", 1)[1].strip()
        ok, msg, path = get_latest_backup_file(filename)
        if ok and path:
            send_document(chat_id, path, caption=f"backup: {path.name}")
        else:
            send_msg(chat_id, f"❌ {msg}")
        return

    # /getlog (레거시 호환)
    if raw.lower().startswith("/getlog"):
        parts = raw.split()
        n = 200
        if len(parts) >= 2:
            try:
                n = int(parts[-1])
            except ValueError:
                pass
        send_msg(chat_id, f"📋 {n}줄 추출 중...")
        ok, msg, path = build_log_extract(n)
        if ok and path:
            send_document(chat_id, path, caption=f"logs {n}줄")
        else:
            send_msg(chat_id, f"❌ {msg}")
        return

    # 인식 못한 텍스트 → 파일명으로 시도
    if raw.endswith(".py"):
        ok, msg, path = find_single_project_file(raw)
        if ok and path:
            send_document(chat_id, path, caption=raw)
            return

    send_msg(chat_id, "❓ 인식 안됨 — /menu 로 메뉴 열기",
             [[btn("🤖 메뉴 열기", "menu")]])

# =========================================================
# 문서 수신 → 자동 배포
# =========================================================

def handle_document(chat_id: int, doc: dict):
    filename = doc.get("file_name") or "unknown"
    file_id = doc.get("file_id")

    if not filename or not file_id:
        send_msg(chat_id, "파일 정보 부족")
        return

    if filename.lower() in DEPLOY_BLOCKED_FILENAMES:
        send_msg(chat_id, f"🚫 차단 파일: {filename}")
        return

    if Path(filename).suffix.lower() not in ALLOWED_DEPLOY_SUFFIXES:
        send_msg(chat_id, f"🚫 .py만 배포 가능: {filename}")
        return

    send_msg(chat_id, f"📥 {filename} 수신 → 배포 시작...")

    try:
        fp = get_file_path(file_id)
        save = TG_INBOX_DIR / filename
        download_telegram_file(fp, save)
        log(f"Downloaded: {save}")

        ok, msg = deploy_file(save)

        if ok:
            moved = move_file(save, TG_DONE_DIR)
            log(f"Done: {moved}")
            send_msg(chat_id, msg,
                     [[btn("📊 상태", "status"), btn("⬅️ 메뉴", "menu")]])
        else:
            moved = move_file(save, TG_FAILED_DIR)
            log(f"Failed: {moved}")
            send_msg(chat_id, f"❌ 배포 실패\n{msg}",
                     [[btn("⬅️ 메뉴", "menu")]])

    except Exception as e:
        send_msg(chat_id, f"❌ 배포 오류: {e}")
        log(f"Deploy error: {e}")

# =========================================================
# 메인 루프
# =========================================================

def main():
    ensure_dirs()
    offset = load_offset()
    log("telegram_deploy_bot v2 started")

    while True:
        try:
            updates = get_updates(offset)

            for upd in updates:
                offset = upd["update_id"] + 1
                save_offset(offset)

                # ★ callback_query 처리 (버튼 클릭)
                cb = upd.get("callback_query")
                if cb:
                    cb_chat = cb.get("message", {}).get("chat", {})
                    cb_chat_id = cb_chat.get("id")
                    cb_msg_id = cb.get("message", {}).get("message_id")
                    cb_data = cb.get("data", "")
                    cb_id = cb.get("id", "")

                    if cb_chat_id != ALLOWED_CHAT_ID:
                        continue

                    handle_callback(cb_chat_id, cb_msg_id, cb_id, cb_data)
                    continue

                # 일반 메시지
                msg = upd.get("message")
                if not msg:
                    continue

                chat_id = msg.get("chat", {}).get("id")
                if chat_id != ALLOWED_CHAT_ID:
                    continue

                if "text" in msg:
                    handle_text(chat_id, msg["text"])
                elif "document" in msg:
                    handle_document(chat_id, msg["document"])
                else:
                    send_msg(chat_id, "텍스트/파일만 처리 가능",
                             [[btn("🤖 메뉴", "menu")]])

        except KeyboardInterrupt:
            log("Stopped by user")
            break
        except Exception as e:
            log(f"Main loop error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
