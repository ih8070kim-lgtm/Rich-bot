import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import filedialog, ttk, messagebox
from openpyxl.utils import get_column_letter

APP_TITLE = "트리니티 로그 추출기"

FILE_MAP = {
    "체결 로그": "log_fills.csv",
    "주문 로그": "log_orders.csv",
    "의도 로그": "log_intents.csv",
    "포지션 로그": "log_positions.csv",
    "리스크 로그": "log_risk.csv",
    "거래 종료 로그": "log_trades.csv",
    "유니버스 로그": "log_universe.csv",
}

ALL_FILE_LABEL = "전체 로그"

TIME_OPTIONS = [
    "최근 1시간",
    "최근 3시간",
    "최근 6시간",
    "최근 12시간",
    "최근 24시간",
    "최근 2일",
    "최근 3일",
    "최근 7일",
]


def parse_time_option(text: str):
    now = datetime.now()
    mapping = {
        "최근 1시간": now - timedelta(hours=1),
        "최근 3시간": now - timedelta(hours=3),
        "최근 6시간": now - timedelta(hours=6),
        "최근 12시간": now - timedelta(hours=12),
        "최근 24시간": now - timedelta(hours=24),
        "최근 2일": now - timedelta(days=2),
        "최근 3일": now - timedelta(days=3),
        "최근 7일": now - timedelta(days=7),
    }
    return mapping[text], now


def safe_read_csv(path: Path) -> pd.DataFrame:
    """
    CSV를 최대한 안전하게 읽는다.
    컬럼 수가 안 맞는 일부 깨진 행이 있어도 가능한 범위에서 로딩.
    """
    try:
        return pd.read_csv(path)
    except Exception:
        try:
            return pd.read_csv(path, engine="python", on_bad_lines="skip")
        except Exception as e:
            raise Exception(f"파일 읽기 실패: {path.name}\n{e}")


def load_selected_logs(folder: Path, selected_label: str):
    logs = {}

    targets = FILE_MAP.copy()
    if selected_label != ALL_FILE_LABEL:
        targets = {selected_label: FILE_MAP[selected_label]}

    for label, filename in targets.items():
        path = folder / filename
        if not path.exists():
            continue

        df = safe_read_csv(path)

        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors="coerce")
            df = df[df["time"].notna()].copy()

        df.insert(0, "로그종류", label)
        logs[label] = df

    return logs


def filter_dataframe(
    df: pd.DataFrame,
    start_dt: datetime,
    end_dt: datetime,
    symbol_text: str,
    keyword_text: str,
):
    temp = df.copy()

    if "time" in temp.columns:
        temp = temp[(temp["time"] >= start_dt) & (temp["time"] <= end_dt)]

    symbol_text = symbol_text.strip()
    if symbol_text:
        if "symbol" in temp.columns:
            temp = temp[
                temp["symbol"].astype(str).str.contains(symbol_text, case=False, na=False)
            ]
        else:
            return pd.DataFrame()

    keyword_text = keyword_text.strip()
    if keyword_text:
        temp = temp[
            temp.astype(str).apply(
                lambda row: row.str.contains(keyword_text, case=False, na=False).any(),
                axis=1,
            )
        ]

    return temp


def autosize_worksheet(worksheet, df: pd.DataFrame):
    """
    openpyxl 방식 열 너비 자동 조정
    """
    for idx, col in enumerate(df.columns, start=1):
        max_len = len(str(col))

        if len(df) > 0:
            try:
                data_max = df[col].astype(str).map(len).max()
                if pd.notna(data_max):
                    max_len = max(max_len, int(data_max))
            except Exception:
                pass

        col_letter = get_column_letter(idx)
        worksheet.column_dimensions[col_letter].width = min(max_len + 2, 40)


def export_to_excel(
    folder: Path,
    selected_label: str,
    time_option: str,
    symbol_text: str,
    keyword_text: str,
):
    start_dt, end_dt = parse_time_option(time_option)
    logs = load_selected_logs(folder, selected_label)

    if not logs:
        raise Exception("선택한 조건에 맞는 로그 파일을 찾지 못했습니다.")

    filtered_map = {}
    total_rows = 0

    for label, df in logs.items():
        result = filter_dataframe(df, start_dt, end_dt, symbol_text, keyword_text)
        if len(result) > 0:
            if "time" in result.columns:
                result = result.sort_values("time")
            filtered_map[label] = result
            total_rows += len(result)

    if total_rows == 0:
        raise Exception("조건에 맞는 데이터가 없습니다.")

    timestamp_text = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = folder / f"로그추출_{timestamp_text}.xlsx"

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        summary_rows = []
        for label, df in filtered_map.items():
            summary_rows.append({
                "로그종류": label,
                "건수": len(df),
            })

        summary_df = pd.DataFrame(summary_rows)
        summary_df.to_excel(writer, sheet_name="요약", index=False)

        for label, df in filtered_map.items():
            sheet_name = label[:31]
            df.to_excel(writer, sheet_name=sheet_name, index=False)

        info_df = pd.DataFrame([
            {"항목": "로그 선택", "값": selected_label},
            {"항목": "시간 범위", "값": time_option},
            {"항목": "시작 시간", "값": start_dt.strftime("%Y-%m-%d %H:%M:%S")},
            {"항목": "종료 시간", "값": end_dt.strftime("%Y-%m-%d %H:%M:%S")},
            {"항목": "심볼 필터", "값": symbol_text or "(없음)"},
            {"항목": "키워드 필터", "값": keyword_text or "(없음)"},
            {"항목": "총 건수", "값": total_rows},
        ])
        info_df.to_excel(writer, sheet_name="추출조건", index=False)

        for sheet_name, ws in writer.sheets.items():
            if sheet_name == "요약":
                autosize_worksheet(ws, summary_df)
            elif sheet_name == "추출조건":
                autosize_worksheet(ws, info_df)
            else:
                original_key = next(
                    (k for k in filtered_map.keys() if k[:31] == sheet_name),
                    None,
                )
                if original_key:
                    autosize_worksheet(ws, filtered_map[original_key])

    return output_path, total_rows, filtered_map


def browse_folder():
    path = filedialog.askdirectory()
    if path:
        folder_var.set(path)


def run_export():
    try:
        folder_text = folder_var.get().strip()
        if not folder_text:
            raise Exception("로그 폴더를 선택하세요.")

        folder = Path(folder_text)
        if not folder.exists() or not folder.is_dir():
            raise Exception("올바른 로그 폴더가 아닙니다.")

        output_path, total_rows, filtered_map = export_to_excel(
            folder=folder,
            selected_label=log_type_var.get(),
            time_option=time_option_var.get(),
            symbol_text=symbol_var.get(),
            keyword_text=keyword_var.get(),
        )

        summary_text = "\n".join([f"- {k}: {len(v)}건" for k, v in filtered_map.items()])
        status_var.set(f"완료 | 총 {total_rows}건")

        messagebox.showinfo(
            "완료",
            f"엑셀 저장 완료\n\n파일: {output_path}\n\n총 건수: {total_rows}건\n\n{summary_text}",
        )

    except Exception as e:
        status_var.set("오류 발생")
        messagebox.showerror("오류", str(e))


root = tk.Tk()
root.title(APP_TITLE)
root.geometry("720x360")
root.resizable(False, False)

folder_var = tk.StringVar()
log_type_var = tk.StringVar(value=ALL_FILE_LABEL)
time_option_var = tk.StringVar(value="최근 24시간")
symbol_var = tk.StringVar()
keyword_var = tk.StringVar()
status_var = tk.StringVar(value="대기 중")

main = ttk.Frame(root, padding=16)
main.pack(fill="both", expand=True)

header = ttk.Label(main, text="트리니티 로그 추출기", font=("맑은 고딕", 16, "bold"))
header.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 14))

row = 1
ttk.Label(main, text="로그 폴더").grid(row=row, column=0, sticky="w", pady=6)
ttk.Entry(main, textvariable=folder_var, width=60).grid(
    row=row, column=1, sticky="we", pady=6, padx=6
)
ttk.Button(main, text="폴더 선택", command=browse_folder).grid(
    row=row, column=2, pady=6
)

row += 1
ttk.Label(main, text="가져올 로그").grid(row=row, column=0, sticky="w", pady=6)
log_type_combo = ttk.Combobox(
    main,
    textvariable=log_type_var,
    values=[ALL_FILE_LABEL] + list(FILE_MAP.keys()),
    state="readonly",
    width=25,
)
log_type_combo.grid(row=row, column=1, sticky="w", pady=6, padx=6)

row += 1
ttk.Label(main, text="시간 범위").grid(row=row, column=0, sticky="w", pady=6)
time_combo = ttk.Combobox(
    main,
    textvariable=time_option_var,
    values=TIME_OPTIONS,
    state="readonly",
    width=25,
)
time_combo.grid(row=row, column=1, sticky="w", pady=6, padx=6)

row += 1
ttk.Label(main, text="심볼 필터").grid(row=row, column=0, sticky="w", pady=6)
ttk.Entry(main, textvariable=symbol_var, width=28).grid(
    row=row, column=1, sticky="w", pady=6, padx=6
)
ttk.Label(main, text="예: INJ / XRP  (비워두면 전체)").grid(
    row=row, column=2, sticky="w", pady=6
)

row += 1
ttk.Label(main, text="키워드 필터").grid(row=row, column=0, sticky="w", pady=6)
ttk.Entry(main, textvariable=keyword_var, width=28).grid(
    row=row, column=1, sticky="w", pady=6, padx=6
)
ttk.Label(main, text="예: ASYM / HEDGE / DCA  (비워두면 전체)").grid(
    row=row, column=2, sticky="w", pady=6
)

row += 1
info_text = "가장 많이 쓰는 방식: 로그 종류 선택 → 시간 범위 선택 → 엑셀로 저장"
info_label = ttk.Label(main, text=info_text)
info_label.grid(row=row, column=0, columnspan=3, sticky="w", pady=(14, 10))

row += 1
button_frame = ttk.Frame(main)
button_frame.grid(row=row, column=0, columnspan=3, sticky="w", pady=8)

ttk.Button(button_frame, text="엑셀 파일 만들기", command=run_export).pack(
    side="left", padx=(0, 8)
)
ttk.Button(button_frame, text="닫기", command=root.destroy).pack(side="left")

row += 1
ttk.Separator(main, orient="horizontal").grid(
    row=row, column=0, columnspan=3, sticky="we", pady=(14, 10)
)

row += 1
ttk.Label(main, textvariable=status_var).grid(
    row=row, column=0, columnspan=3, sticky="w"
)

main.columnconfigure(1, weight=1)

root.mainloop()