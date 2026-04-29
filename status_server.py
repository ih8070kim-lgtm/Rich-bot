"""
★ V10.31AM3 hotfix-18: Trinity 라이브 대시보드 서버 + 차트/마커
표준 라이브러리만 사용 (lightweight-charts는 CDN, ccxt는 lazy import)

사용법:
  python3 status_server.py [port]     (기본 포트: 7777)

API 엔드포인트:
  GET /                  — 대시보드 HTML
  GET /api/status        — v9_status.json (3초 갱신)
  GET /api/chart?sym=X   — 활성: v9_chart.json, 비활성: ccxt lazy fetch
  GET /api/markers?sym=X — log_fills + log_trades에서 추출 (모든 심볼)
  GET /api/closed_syms   — 최근 7일 청산 심볼 목록 (활성 제외)

봇 영향 0:
  - 별도 subprocess (main.py가 daemon thread로 띄움)
  - JSON/CSV 파일 read-only
  - ccxt는 비활성 심볼 클릭 시에만 lazy import + fetch (모듈 로드 첫 1회만 0.5~1초 지연)
  - ThreadingHTTPServer → ccxt 호출이 다른 polling 요청 블록 안 함

EC2 보안그룹에서 해당 포트 열어야 외부 접근 가능.
"""
import http.server
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7777
_BASE = os.path.dirname(os.path.abspath(__file__))
STATUS_FILE = os.path.join(_BASE, "v9_status.json")
CHART_FILE  = os.path.join(_BASE, "v9_chart.json")
LOG_DIR     = os.path.join(_BASE, "v9_logs")

# ── ccxt lazy ─────────────────────────────────────────────────────
# 비활성 심볼 클릭 시에만 import. 평상시 메모리 영향 0.
_LAZY_CCXT = None

def _get_lazy_ccxt():
    global _LAZY_CCXT
    if _LAZY_CCXT is None:
        try:
            import ccxt
            _LAZY_CCXT = ccxt.binance({
                'options': {'defaultType': 'future'},
                'enableRateLimit': True,
            })
        except Exception as e:
            print(f"[dashboard] ccxt import 실패: {e}")
            return None
    return _LAZY_CCXT


# ── 마커 추출 (status_writer와 동일 로직 — 의존성 회피 위해 복제) ─────
def _tail_lines(filepath: str, n: int = 30) -> list:
    try:
        with open(filepath, 'rb') as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return []
            chunk = min(fsize, n * 400)
            f.seek(max(0, fsize - chunk))
            data = f.read().decode('utf-8', errors='replace')
            return data.splitlines()[-n:]
    except Exception:
        return []


def _parse_time_ms(ts_str: str) -> int:
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except Exception:
        return 0


def _classify_reason(reason: str) -> str:
    if not reason:
        return "exit_close"
    r = reason.upper()
    if "TRIM" in r:    return "exit_trim"
    if "TP1" in r:     return "exit_tp"
    if "HARD_SL" in r or "FORCE" in r: return "exit_sl"
    if "TRAIL" in r:   return "exit_trail"
    return "exit_close"


def extract_markers(sym_short: str, days: int = 7) -> list:
    """log_fills + log_trades에서 마커 추출. status_writer._extract_markers 복제."""
    sym_full = f"{sym_short}/USDT"
    cutoff = time.time() - days * 86400
    out = []

    fills_file = os.path.join(LOG_DIR, "log_fills.csv")
    if os.path.exists(fills_file):
        for line in _tail_lines(fills_file, 10000):
            cols = line.strip().split(",")
            if len(cols) < 8 or not cols[0].startswith("2026") or cols[2] != sym_full:
                continue
            try:
                t_ms = _parse_time_ms(cols[0])
                if t_ms == 0 or t_ms / 1000 < cutoff:
                    continue
                tag = cols[7]
                price = float(cols[4] or 0)
                if price <= 0:
                    continue
                if tag.startswith("V9_OPEN_"):
                    out.append({"t": t_ms, "type": "entry", "side": cols[3],
                                "price": price, "qty": float(cols[5] or 0),
                                "reason": "OPEN", "tier": 1})
                elif tag.startswith("V9_DCA_PRE_"):
                    tier = 3 if "T3" in cols[1] else 2
                    out.append({"t": t_ms, "type": "dca", "side": cols[3],
                                "price": price, "qty": float(cols[5] or 0),
                                "reason": f"DCA_T{tier}", "tier": tier})
            except (ValueError, IndexError):
                continue

    trades_file = os.path.join(LOG_DIR, "log_trades.csv")
    if os.path.exists(trades_file):
        for line in _tail_lines(trades_file, 3000):
            cols = line.strip().split(",")
            if len(cols) < 12 or not cols[0].startswith("2026") or cols[2] != sym_full:
                continue
            try:
                t_ms = _parse_time_ms(cols[0])
                if t_ms == 0 or t_ms / 1000 < cutoff:
                    continue
                reason = cols[11]
                if reason in ("", "GHOST_CLEANUP"):
                    continue
                exit_price = float(cols[5] or 0)
                if exit_price <= 0:
                    continue
                pnl = float(cols[7] or 0)
                tier = int(cols[9] or 1)
                close_side = "sell" if cols[3] == "buy" else "buy"
                out.append({"t": t_ms, "type": _classify_reason(reason),
                            "side": close_side, "price": exit_price,
                            "pnl": round(pnl, 2), "reason": reason, "tier": tier})
            except (ValueError, IndexError):
                continue

    out.sort(key=lambda x: x["t"])
    return out


def get_closed_symbols(days: int = 7, exclude: set = None) -> list:
    """log_trades.csv에서 최근 N일 청산된 심볼 목록 (PnL/거래수 합산).
    
    exclude: 활성 포지션 심볼 set (대시보드는 활성 카드와 청산 카드 분리)
    Returns: [{"sym":"XLM","trades":N,"pnl":F,"wins":N,"last_t":ms}, ...]
    """
    exclude = exclude or set()
    cutoff = time.time() - days * 86400
    trades_file = os.path.join(LOG_DIR, "log_trades.csv")
    agg = {}
    if not os.path.exists(trades_file):
        return []
    for line in _tail_lines(trades_file, 5000):
        cols = line.strip().split(",")
        if len(cols) < 12 or not cols[0].startswith("2026"):
            continue
        try:
            t_ms = _parse_time_ms(cols[0])
            if t_ms == 0 or t_ms / 1000 < cutoff:
                continue
            reason = cols[11]
            if reason in ("", "GHOST_CLEANUP"):
                continue
            sym_short = cols[2].replace("/USDT", "")
            if sym_short in exclude:
                continue
            pnl = float(cols[7] or 0)
            d = agg.setdefault(sym_short, {"sym": sym_short, "trades": 0,
                                           "pnl": 0.0, "wins": 0, "last_t": 0})
            d["trades"] += 1
            d["pnl"] += pnl
            if pnl > 0:
                d["wins"] += 1
            if t_ms > d["last_t"]:
                d["last_t"] = t_ms
        except (ValueError, IndexError):
            continue
    result = list(agg.values())
    for r in result:
        r["pnl"] = round(r["pnl"], 2)
        r["wr"] = round(r["wins"] / r["trades"] * 100) if r["trades"] else 0
    result.sort(key=lambda x: x["last_t"], reverse=True)
    return result


def fetch_chart_for_sym(sym_short: str) -> dict:
    """차트 데이터 — 활성 심볼은 v9_chart.json, 비활성은 ccxt lazy fetch.
    
    Returns: {"source":"memory|live", "ohlcv":{tf:[[ts,o,h,l,c,v]]}, "markers":[...]}
    """
    # 1. v9_chart.json 시도 (활성 심볼)
    if os.path.exists(CHART_FILE):
        try:
            with open(CHART_FILE, 'r') as f:
                cd = json.load(f)
            ohlcv = cd.get("ohlcv", {}).get(sym_short)
            if ohlcv:
                return {
                    "source": "memory",
                    "ohlcv": ohlcv,
                    "markers": cd.get("markers", {}).get(sym_short) or extract_markers(sym_short, 7),
                    "ts": cd.get("ts", time.time()),
                }
        except Exception:
            pass

    # 2. ccxt lazy fetch (비활성 심볼)
    sym_full = f"{sym_short}/USDT"
    ex = _get_lazy_ccxt()
    if ex is None:
        return {"error": "ccxt unavailable", "markers": extract_markers(sym_short, 7)}
    try:
        ohlcv = {
            "5m":  ex.fetch_ohlcv(sym_full, "5m",  limit=500),  # 약 41시간
            "15m": ex.fetch_ohlcv(sym_full, "15m", limit=500),  # 약 5일
            "1h":  ex.fetch_ohlcv(sym_full, "1h",  limit=200),  # 약 8일
        }
        return {
            "source": "live",
            "ohlcv": ohlcv,
            "markers": extract_markers(sym_short, 7),
            "ts": time.time(),
        }
    except Exception as e:
        return {"error": str(e), "markers": extract_markers(sym_short, 7)}


# ─── 대시보드 HTML ─────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Trinity Live</title>
<script src="https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e17;--card:#111827;--border:#1e293b;--g:#22c55e;--r:#ef4444;--a:#f59e0b;--b:#3b82f6;--c:#06b6d4;--p:#a855f7;--t:#e2e8f0;--m:#64748b;--d:#334155}
body{background:var(--bg);color:var(--t);font-family:-apple-system,system-ui,sans-serif;padding:12px;max-width:480px;margin:0 auto;-webkit-tap-highlight-color:transparent}
.hdr{display:flex;align-items:center;gap:8px;margin-bottom:14px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--g);box-shadow:0 0 8px var(--g);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.hdr h1{font-size:16px;letter-spacing:1px;font-weight:700}
.hdr .ts{margin-left:auto;font-size:10px;color:var(--m)}
.kpis{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:12px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px}
.kpi .l{font-size:10px;color:var(--m);text-transform:uppercase;letter-spacing:.8px}
.kpi .v{font-size:20px;font-weight:700;margin-top:3px;font-family:'SF Mono',monospace}
.kpi .s{font-size:10px;color:var(--m);margin-top:2px}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:10px}
.card h3{font-size:12px;font-weight:600;margin-bottom:8px;color:var(--t);display:flex;align-items:center;gap:6px}
.card h3 .sub{font-size:10px;color:var(--m);font-weight:400;margin-left:auto}
.pos{display:flex;align-items:center;gap:6px;margin-bottom:5px;font-size:12px;cursor:pointer;padding:2px;border-radius:4px}
.pos:hover{background:#1e293b}
.pos .sym{width:42px;font-weight:600;font-family:'SF Mono',monospace}
.pos .badge{font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600;text-align:center;min-width:32px}
.pos .side-l{background:#16a34a22;color:var(--g)}
.pos .side-s{background:#dc262622;color:var(--r)}
.pos .t1{background:#06b6d422;color:var(--c)}
.pos .t2{background:#f59e0b22;color:var(--a)}
.pos .t3{background:#ef444422;color:var(--r)}
.pos .bar-wrap{flex:1;height:14px;background:var(--d);border-radius:4px;overflow:hidden;position:relative}
.pos .bar{height:100%;border-radius:4px;transition:width .5s}
.pos .roi{width:50px;text-align:right;font-weight:600;font-family:'SF Mono',monospace;font-size:11px}
.closed-row{display:grid;grid-template-columns:60px 1fr 60px 50px 30px;gap:6px;padding:6px 4px;border-bottom:1px solid var(--d);font-size:11px;cursor:pointer;align-items:center}
.closed-row:hover{background:#1e293b}
.closed-row .csym{font-weight:600;font-family:'SF Mono',monospace}
.closed-row .cmeta{font-size:10px;color:var(--m)}
.closed-row .cpnl{text-align:right;font-family:'SF Mono',monospace;font-weight:600}
.closed-row .cwr{font-family:'SF Mono',monospace;color:var(--m);font-size:10px;text-align:right}
.closed-row .cn{font-family:'SF Mono',monospace;color:var(--m);font-size:10px;text-align:right}
.trades{max-height:280px;overflow-y:auto}
.trade{display:flex;gap:6px;padding:4px 0;border-bottom:1px solid var(--d);font-size:11px;align-items:center}
.trade:last-child{border:none}
.trade .tt{color:var(--m);width:80px;font-size:10px}
.trade .ts2{font-weight:600;width:40px;font-family:'SF Mono',monospace}
.trade .tr{flex:1;font-size:10px;color:var(--m)}
.trade .tp{font-weight:600;font-family:'SF Mono',monospace;width:50px;text-align:right}
.skew-bar{height:20px;background:var(--d);border-radius:10px;position:relative;overflow:hidden;margin-top:6px}
.skew-fill{height:100%;border-radius:10px;transition:width .5s}
.tab-bar{display:flex;gap:4px;margin-bottom:10px}
.tab{flex:1;text-align:center;padding:7px;font-size:11px;font-weight:600;border:1px solid var(--d);border-radius:6px;cursor:pointer;color:var(--m);background:transparent;text-transform:uppercase}
.tab.active{background:var(--b);color:#fff;border-color:var(--b)}
.err{text-align:center;padding:40px;color:var(--r);font-size:13px}
.stale{color:var(--a) !important}
.refresh-note{text-align:center;font-size:9px;color:var(--d);margin-top:8px}
.chart-mini{width:100%;height:160px;background:#0a0e17;border-radius:6px}
/* ── 모달 (전체화면 차트) ── */
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.85);z-index:1000;display:none;flex-direction:column}
.modal-overlay.open{display:flex}
.modal-hdr{padding:12px 14px;background:#111827;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.modal-hdr .msym{font-weight:700;font-size:16px;font-family:'SF Mono',monospace}
.modal-hdr .mmeta{font-size:11px;color:var(--m)}
.modal-hdr .mclose{margin-left:auto;background:transparent;border:1px solid var(--d);color:var(--t);padding:4px 12px;border-radius:4px;font-size:14px;cursor:pointer}
.tf-bar{display:flex;gap:4px;padding:8px 12px;background:#0a0e17;border-bottom:1px solid var(--border)}
.tf-btn{flex:1;padding:6px;font-size:11px;font-weight:600;border:1px solid var(--d);border-radius:4px;background:transparent;color:var(--m);cursor:pointer}
.tf-btn.active{background:var(--b);color:#fff;border-color:var(--b)}
.modal-chart{flex:1;width:100%;background:#0a0e17}
.modal-legend{padding:8px 12px;background:#111827;border-top:1px solid var(--border);font-size:10px;color:var(--m);display:flex;flex-wrap:wrap;gap:8px}
.legend-item{display:flex;align-items:center;gap:3px}
.legend-dot{width:8px;height:8px;border-radius:50%}
.modal-loading{flex:1;display:flex;align-items:center;justify-content:center;color:var(--m);font-size:13px}
</style>
</head>
<body>
<div class="hdr">
  <div class="dot" id="dot"></div>
  <h1>TRINITY</h1>
  <span class="ts" id="ts">--</span>
</div>
<div class="kpis" id="kpis"></div>
<div class="tab-bar">
  <div class="tab active" onclick="setTab('pos')" id="tab-pos">포지션</div>
  <div class="tab" onclick="setTab('trades')" id="tab-trades">트레이드</div>
  <div class="tab" onclick="setTab('skew')" id="tab-skew">리스크</div>
  <div class="tab" onclick="setTab('today')" id="tab-today">오늘</div>
  <div class="tab" onclick="setTab('insight')" id="tab-insight">인사이트</div>
</div>
<div id="content"></div>
<div class="refresh-note">3초마다 자동 갱신</div>

<!-- 차트 모달 -->
<div class="modal-overlay" id="modal">
  <div class="modal-hdr">
    <span class="msym" id="m-sym">--</span>
    <span class="mmeta" id="m-meta"></span>
    <button class="mclose" onclick="closeModal()">✕</button>
  </div>
  <div class="tf-bar">
    <button class="tf-btn" data-tf="5m" onclick="setTF('5m')">5m</button>
    <button class="tf-btn active" data-tf="15m" onclick="setTF('15m')">15m</button>
    <button class="tf-btn" data-tf="1h" onclick="setTF('1h')">1h</button>
  </div>
  <div class="modal-chart" id="m-chart"></div>
  <div class="modal-loading" id="m-loading" style="display:none">차트 로딩중...</div>
  <div class="modal-legend">
    <span class="legend-item"><span class="legend-dot" style="background:#06b6d4"></span>진입</span>
    <span class="legend-item"><span class="legend-dot" style="background:#0ea5e9"></span>DCA</span>
    <span class="legend-item"><span class="legend-dot" style="background:#22c55e"></span>TP1</span>
    <span class="legend-item"><span class="legend-dot" style="background:#f59e0b"></span>TRIM</span>
    <span class="legend-item"><span class="legend-dot" style="background:#a855f7"></span>TRAIL</span>
    <span class="legend-item"><span class="legend-dot" style="background:#ef4444"></span>SL</span>
    <span class="legend-item"><span class="legend-dot" style="background:#94a3b8"></span>CLOSE</span>
  </div>
</div>

<script>
let currentTab='pos', data=null, closedSyms=[];
let modalSym=null, modalTF='15m', modalChart=null, modalSeries=null, modalChartData=null;
let btcChart=null, btcSeries=null;
let balChart=null, balSeries=null;

function setTab(t){
  currentTab=t;
  document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));
  document.getElementById('tab-'+t).classList.add('active');
  if(t!=='pos' && btcChart){ try{btcChart.remove();}catch(e){} btcChart=null; }
  if(t!=='today' && balChart){ try{balChart.remove();}catch(e){} balChart=null; }
  render();
}

function $(id){return document.getElementById(id)}
function c(v,pos){return v>0?(pos||'var(--g)'):(v<0?'var(--r)':'var(--m)')}

async function fetchData(){
  try{
    const r=await fetch('/api/status?t='+Date.now());
    if(!r.ok) throw new Error(r.status);
    data=await r.json();
    if(currentTab==='pos'){
      try{
        const r2=await fetch('/api/closed_syms?t='+Date.now());
        if(r2.ok) closedSyms=await r2.json();
      }catch(e){}
    }
    render();
  }catch(e){
    $('content').innerHTML='<div class="err">연결 대기중...</div>';
    $('dot').style.background='var(--r)';
    $('dot').style.boxShadow='0 0 8px var(--r)';
  }
}

function render(){
  if(!data) return;
  const age=(Date.now()/1000)-data.ts;
  const stale=age>15;
  $('ts').textContent=data.time.slice(11,19)+' UTC';
  $('ts').className='ts'+(stale?' stale':'');
  $('dot').style.background=stale?'var(--a)':'var(--g)';
  $('dot').style.boxShadow='0 0 8px '+(stale?'var(--a)':'var(--g)');

  const s=data.summary, td=data.today;
  $('kpis').innerHTML=`
    <div class="kpi"><div class="l">Balance</div><div class="v">$${data.balance.toLocaleString()}</div><div class="s">MR ${(data.margin_ratio*100).toFixed(1)}%</div></div>
    <div class="kpi"><div class="l">미실현</div><div class="v" style="color:${c(s.unrealized_pnl)}">${s.unrealized_pnl>0?'+':''}$${s.unrealized_pnl.toFixed(1)}</div><div class="s">${s.total_positions}포지션 (L${s.long} S${s.short})</div></div>
    <div class="kpi"><div class="l">오늘 PnL</div><div class="v" style="color:${c(td.pnl)}">${td.pnl>0?'+':''}$${td.pnl.toFixed(1)}</div><div class="s">${td.trades}건 WR${td.wr}%${td.ghost?` <span style="color:var(--a)">G${td.ghost}</span>`:''}${td.balance_diff!==null && td.balance_diff!==undefined ? ` · 잔고${td.balance_diff>0?'+':''}$${td.balance_diff.toFixed(1)}` : ''}</div></div>
    <div class="kpi"><div class="l">Urgency</div><div class="v" style="color:${data.skew.urgency>30?'var(--r)':data.skew.urgency>15?'var(--a)':'var(--c)'}">${data.skew.urgency||0}</div><div class="s">${data.skew.heavy_side||'-'} ${data.skew.heavy_roi||0}%</div></div>
  `;

  const ptp = data.ptp || {};
  let ptpBadge = '';
  if(ptp.state === 'active'){
    const stepLabel = ptp.last_step >= 0 ? `STEP ${ptp.last_step + 1}/4` : 'STARTED';
    ptpBadge = `<div style="margin:6px 0;padding:8px 12px;background:#3b1e1e;border-left:3px solid var(--r);border-radius:4px;font-size:12px">
      <span style="color:var(--r);font-weight:700">🔴 PTP ACTIVE — ${stepLabel}</span>
      <span style="color:var(--m);margin-left:8px">peak +${ptp.peak_gain_pct.toFixed(2)}% · drop ${ptp.current_drop_pct.toFixed(2)}%p</span>
    </div>`;
  } else if(ptp.state === 'armed'){
    const dropBar = ptp.drop_thresh_pct > 0 ? Math.min(ptp.current_drop_pct / ptp.drop_thresh_pct * 100, 100) : 0;
    ptpBadge = `<div style="margin:6px 0;padding:8px 12px;background:#1e2e3b;border-left:3px solid var(--a);border-radius:4px;font-size:12px">
      <span style="color:var(--a);font-weight:700">🟡 PTP ARMED</span>
      <span style="color:var(--m);margin-left:8px">peak +${ptp.peak_gain_pct.toFixed(2)}% · drop ${ptp.current_drop_pct.toFixed(2)}%p / ${ptp.drop_thresh_pct.toFixed(2)}%p</span>
      <div style="margin-top:4px;background:#0f172a;border-radius:3px;height:4px;overflow:hidden"><div style="width:${dropBar}%;height:100%;background:var(--a);opacity:.7"></div></div>
    </div>`;
  } else {
    ptpBadge = `<div style="margin:6px 0;padding:6px 12px;background:#1e293b;border-radius:4px;font-size:11px;color:var(--m)">
      ⚪ PTP ${ptp.state||'idle'} · peak +${ptp.peak_gain_pct?ptp.peak_gain_pct.toFixed(2):'0.00'}% (arm ${ptp.arm_trig_pct!==undefined?ptp.arm_trig_pct.toFixed(1):'?'}%, drop ${ptp.drop_thresh_pct?ptp.drop_thresh_pct.toFixed(1):'?'}%p)
    </div>`;
  }
  const oldBadge = document.getElementById('ptp-badge');
  if(oldBadge) oldBadge.remove();
  const badgeDiv = document.createElement('div');
  badgeDiv.id = 'ptp-badge';
  badgeDiv.innerHTML = ptpBadge;
  $('kpis').parentNode.insertBefore(badgeDiv, $('kpis').nextSibling);

  if(currentTab==='pos') renderPos();
  else if(currentTab==='trades') renderTrades();
  else if(currentTab==='skew') renderSkew();
  else if(currentTab==='today') renderToday();
  else if(currentTab==='insight') renderInsight();
}

// ─── 포지션 탭 ──────────────────────────────────────────────────────
function renderPos(){
  let html='';
  html+='<div class="card"><h3>BTC <span class="sub">5m × 60</span></h3><div id="btc-mini" class="chart-mini"></div></div>';
  html+='<div class="card"><h3>활성 포지션 <span class="sub">'+(data.positions||[]).length+'개 · 클릭→차트</span></h3>';
  if(!data.positions || !data.positions.length){
    html+='<div style="color:var(--m);font-size:12px;padding:8px">활성 포지션 없음</div>';
  } else {
    for(const p of data.positions){
      const sideC=p.side==='LONG'?'side-l':'side-s';
      const tierC='t'+p.tier;
      const barW=Math.min(Math.abs(p.roi)*5,100);
      const barC=p.roi>0?'var(--g)':'var(--r)';
      html+=`<div class="pos" onclick="openModal('${p.sym}','active')">
        <span class="sym">${p.sym}</span>
        <span class="badge ${sideC}">${p.side==='LONG'?'L':'S'}</span>
        <span class="badge ${tierC}">T${p.tier}</span>
        <div class="bar-wrap"><div class="bar" style="width:${barW}%;background:${barC};${p.roi<0?'margin-left:auto':''}"></div></div>
        <span class="roi" style="color:${c(p.roi)}">${p.roi>0?'+':''}${p.roi.toFixed(1)}%</span>
      </div>`;
    }
  }
  html+='</div>';
  html+='<div class="card"><h3>청산 심볼 <span class="sub">7일 · '+(closedSyms||[]).length+'종목 · 클릭→차트</span></h3>';
  if(!closedSyms || !closedSyms.length){
    html+='<div style="color:var(--m);font-size:12px;padding:8px">청산 데이터 없음</div>';
  } else {
    for(const s of closedSyms){
      const pnlC=s.pnl>=0?'var(--g)':'var(--r)';
      const ago=fmtAgo(Date.now()-s.last_t);
      html+=`<div class="closed-row" onclick="openModal('${s.sym}','closed')">
        <span class="csym">${s.sym}</span>
        <span class="cmeta">${ago} 전</span>
        <span class="cpnl" style="color:${pnlC}">${s.pnl>0?'+':''}$${s.pnl.toFixed(1)}</span>
        <span class="cwr">WR${s.wr}%</span>
        <span class="cn">${s.trades}건</span>
      </div>`;
    }
  }
  html+='</div>';
  $('content').innerHTML=html;
  drawBtcMini();
}

function fmtAgo(ms){
  const m=Math.floor(ms/60000);
  if(m<60) return m+'분';
  const h=Math.floor(m/60);
  if(h<24) return h+'시간';
  return Math.floor(h/24)+'일';
}

async function drawBtcMini(){
  const el=$('btc-mini');
  if(!el || !window.LightweightCharts) return;
  try{
    if(btcChart){ try{btcChart.remove();}catch(e){} btcChart=null; }
    const r=await fetch('/api/chart?sym=BTC&t='+Date.now());
    if(!r.ok) return;
    const cd=await r.json();
    const series5m=(cd.ohlcv && cd.ohlcv['5m']) || [];
    if(!series5m.length){ el.innerHTML='<div style="padding:30px;text-align:center;color:var(--m);font-size:11px">BTC 데이터 로딩 대기...</div>'; return; }
    btcChart=LightweightCharts.createChart(el,{
      width:el.clientWidth, height:160,
      layout:{background:{color:'#0a0e17'},textColor:'#64748b'},
      grid:{vertLines:{color:'#1e293b'},horzLines:{color:'#1e293b'}},
      timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#334155'},
      rightPriceScale:{borderColor:'#334155'},
      handleScroll:false, handleScale:false,
    });
    btcSeries=btcChart.addCandlestickSeries({
      upColor:'#22c55e',downColor:'#ef4444',borderVisible:false,
      wickUpColor:'#22c55e',wickDownColor:'#ef4444',
    });
    const cdata=series5m.map(c=>({time:Math.floor(c[0]/1000),open:c[1],high:c[2],low:c[3],close:c[4]}));
    btcSeries.setData(cdata);
    btcChart.timeScale().fitContent();
  }catch(e){ console.error('btc mini',e); }
}

// ─── 모달 (차트 전체화면) ──────────────────────────────────────────
async function openModal(sym, mode){
  modalSym=sym;
  modalTF='15m';
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.toggle('active', b.dataset.tf===modalTF));
  $('m-sym').textContent=sym+'/USDT';
  $('m-meta').textContent=mode==='active'?'활성 포지션':'청산 심볼';
  $('modal').classList.add('open');
  $('m-loading').style.display='flex';
  $('m-loading').textContent='차트 로딩중...';
  $('m-chart').style.display='none';
  try{
    const r=await fetch('/api/chart?sym='+encodeURIComponent(sym)+'&t='+Date.now());
    if(!r.ok) throw new Error('chart fetch '+r.status);
    modalChartData=await r.json();
    if(modalChartData.error){
      $('m-loading').textContent='차트 로딩 실패: '+modalChartData.error;
      return;
    }
    $('m-loading').style.display='none';
    $('m-chart').style.display='block';
    $('m-meta').textContent=(mode==='active'?'활성 · ':'청산 · ')+
      (modalChartData.source==='memory'?'봇 메모리':'거래소 직접');
    drawModalChart();
  }catch(e){
    $('m-loading').textContent='로딩 실패: '+e.message;
    console.error('modal',e);
  }
}

function setTF(tf){
  modalTF=tf;
  document.querySelectorAll('.tf-btn').forEach(b=>b.classList.toggle('active', b.dataset.tf===tf));
  drawModalChart();
}

function closeModal(){
  $('modal').classList.remove('open');
  if(modalChart){ try{modalChart.remove();}catch(e){} modalChart=null; }
  modalChartData=null;
}

function drawModalChart(){
  if(!modalChartData || !window.LightweightCharts) return;
  const el=$('m-chart');
  if(modalChart){ try{modalChart.remove();}catch(e){} modalChart=null; }
  const series=(modalChartData.ohlcv && modalChartData.ohlcv[modalTF]) || [];
  if(!series.length){
    el.innerHTML='<div style="padding:60px;text-align:center;color:var(--m);font-size:13px">'+modalTF+' 데이터 없음</div>';
    return;
  }
  el.innerHTML='';
  modalChart=LightweightCharts.createChart(el,{
    width:el.clientWidth, height:el.clientHeight,
    layout:{background:{color:'#0a0e17'},textColor:'#94a3b8'},
    grid:{vertLines:{color:'#1e293b'},horzLines:{color:'#1e293b'}},
    timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#334155'},
    rightPriceScale:{borderColor:'#334155'},
    crosshair:{mode:LightweightCharts.CrosshairMode.Normal},
  });
  modalSeries=modalChart.addCandlestickSeries({
    upColor:'#22c55e',downColor:'#ef4444',borderVisible:false,
    wickUpColor:'#22c55e',wickDownColor:'#ef4444',
  });
  const cdata=series.map(c=>({time:Math.floor(c[0]/1000),open:c[1],high:c[2],low:c[3],close:c[4]}));
  modalSeries.setData(cdata);
  const markers=(modalChartData.markers||[]).map(buildMarker).filter(Boolean);
  // ★ lightweight-charts setMarkers는 시간 오름차순 필수
  markers.sort((a,b)=>a.time-b.time);
  if(markers.length) modalSeries.setMarkers(markers);
  modalChart.timeScale().fitContent();
  setTimeout(()=>{ if(modalChart) modalChart.applyOptions({width:el.clientWidth, height:el.clientHeight}); }, 50);
}

function buildMarker(m){
  if(!m || !m.t) return null;
  const cfg = {
    entry:      {color:'#06b6d4', shape:'arrowUp',   txt:'IN'},
    dca:        {color:'#0ea5e9', shape:'arrowUp',   txt:'D'+m.tier},
    exit_tp:    {color:'#22c55e', shape:'arrowDown', txt:'TP1'},
    exit_trim:  {color:'#f59e0b', shape:'arrowDown', txt:'TR'+m.tier},
    exit_trail: {color:'#a855f7', shape:'arrowDown', txt:'TRL'},
    exit_sl:    {color:'#ef4444', shape:'arrowDown', txt:'SL'},
    exit_close: {color:'#94a3b8', shape:'circle',    txt:'CL'},
  }[m.type] || {color:'#94a3b8', shape:'circle', txt:'?'};
  // entry/dca: 매수면 봉 아래 ▲ (가격 상승 기대), 매도면 봉 위 ▼ 형태로 표시
  let pos;
  if(m.type==='entry' || m.type==='dca'){
    pos = m.side==='buy' ? 'belowBar' : 'aboveBar';
  } else {
    // 청산: 청산 사이드와 무관하게 봉 위에 ▼ 통일 (가독성)
    pos = 'aboveBar';
  }
  let text = cfg.txt;
  if(m.pnl !== undefined && m.pnl !== null) text += ` ${m.pnl>0?'+':''}$${m.pnl.toFixed(1)}`;
  return {
    time: Math.floor(m.t/1000),
    position: pos,
    color: cfg.color,
    shape: cfg.shape,
    text: text,
  };
}

// ─── 트레이드 탭 ────────────────────────────────────────────────────
function renderTrades(){
  let html='<div class="card"><h3>최근 트레이드 <span class="sub">최근 15건</span></h3><div class="trades">';
  const tr=(data.recent_trades||[]).slice().reverse();
  if(!tr.length) html+='<div style="color:var(--m);font-size:12px;padding:8px">트레이드 없음</div>';
  for(const t of tr){
    const pnlC=t.pnl>=0?'var(--g)':'var(--r)';
    html+=`<div class="trade">
      <span class="tt">${t.time}</span>
      <span class="ts2">${t.sym}</span>
      <span class="badge ${t.side==='buy'?'side-l':'side-s'}">${t.side==='buy'?'L':'S'}</span>
      <span class="tr">${t.reason} T${t.tier}</span>
      <span class="tp" style="color:${pnlC}">${t.pnl>0?'+':''}$${t.pnl.toFixed(2)}</span>
    </div>`;
  }
  html+='</div></div>';
  $('content').innerHTML=html;
}

// ─── 리스크 탭 ──────────────────────────────────────────────────────
function renderSkew(){
  const sk=data.skew, s=data.summary;
  const skewPct=Math.abs(sk.skew_pct||0);
  const urgPct=Math.min((sk.urgency||0)/60*100,100);
  let html='<div class="card"><h3>스큐 & 리스크</h3>';
  html+=`<div style="font-size:11px;color:var(--m);margin-bottom:4px">Skew ${(sk.skew_pct||0).toFixed(1)}% (${sk.heavy_side||'-'} 편중)</div>`;
  html+=`<div class="skew-bar"><div class="skew-fill" style="width:${Math.min(skewPct*2,100)}%;background:${skewPct>25?'var(--r)':skewPct>15?'var(--a)':'var(--g)'}"></div></div>`;
  html+=`<div style="font-size:11px;color:var(--m);margin-top:10px;margin-bottom:4px">Urgency ${(sk.urgency||0).toFixed(0)}/60</div>`;
  html+=`<div class="skew-bar"><div class="skew-fill" style="width:${urgPct}%;background:${sk.urgency>30?'var(--r)':sk.urgency>15?'var(--a)':'var(--c)'}"></div></div>`;
  html+=`<div style="margin-top:12px;font-size:11px">
    <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--d)"><span style="color:var(--g)">LONG ×${s.long}</span><span style="font-family:'SF Mono',monospace">$${s.long_notional.toFixed(0)}</span></div>
    <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--d)"><span style="color:var(--r)">SHORT ×${s.short}</span><span style="font-family:'SF Mono',monospace">$${s.short_notional.toFixed(0)}</span></div>
    <div style="display:flex;justify-content:space-between;padding:4px 0"><span style="color:var(--m)">Margin Ratio</span><span style="font-family:'SF Mono',monospace;color:${data.margin_ratio>0.8?'var(--r)':data.margin_ratio>0.6?'var(--a)':'var(--t)'}">${(data.margin_ratio*100).toFixed(1)}%</span></div>
  </div></div>`;
  html+='<div class="card"><h3>포지션 상세</h3>';
  for(const p of data.positions){
    const holdH=(p.hold_min/60).toFixed(1);
    html+=`<div style="display:flex;gap:6px;padding:5px 0;border-bottom:1px solid var(--d);font-size:11px;align-items:center">
      <span style="width:40px;font-weight:600;font-family:'SF Mono',monospace">${p.sym}</span>
      <span style="flex:1;color:var(--m)">$${p.notional.toFixed(0)} · ${holdH}h · ${p.entry_type}</span>
      <span style="color:${c(p.worst_roi)};font-family:'SF Mono',monospace;font-size:10px">W${p.worst_roi.toFixed(1)}</span>
      <span style="color:${c(p.roi)};font-weight:600;font-family:'SF Mono',monospace">${p.roi>0?'+':''}${p.roi.toFixed(1)}%</span>
    </div>`;
  }
  html+='</div>';
  $('content').innerHTML=html;
}

// ─── 오늘 탭 ────────────────────────────────────────────────────────
function renderToday(){
  let html='';
  html+='<div class="card"><h3>잔고 <span class="sub">최근 24h</span></h3><div id="bal-chart" class="chart-mini" style="height:180px"></div></div>';
  const daily=data.daily||[];
  html+='<div class="card"><h3>일별 PnL <span class="sub">최근 7일</span></h3>';
  if(!daily.length) html+='<div style="color:var(--m);font-size:12px;padding:8px">데이터 없음</div>';
  for(const d of daily){
    const pnlC=d.pnl>=0?'var(--g)':'var(--r)';
    html+=`<div style="display:flex;padding:5px 0;border-bottom:1px solid var(--d);font-size:12px;align-items:center">
      <span style="width:60px;font-family:'SF Mono',monospace">${d.date}</span>
      <span style="flex:1;color:var(--m);font-size:10px">${d.trades}건 WR${d.wr}%</span>
      <span style="font-family:'SF Mono',monospace;font-weight:600;color:${pnlC}">${d.pnl>0?'+':''}$${d.pnl.toFixed(1)}</span>
    </div>`;
  }
  html+='</div>';
  const hp=data.hour_pnl||[];
  html+='<div class="card"><h3>시간대별 PnL (오늘 UTC)</h3>';
  const maxAbs=Math.max(...hp.map(Math.abs),1);
  html+='<div style="display:flex;align-items:flex-end;height:80px;gap:1px;padding:4px 0">';
  for(let h=0;h<24;h++){
    const v=hp[h]||0;
    const hAbs=Math.abs(v)/maxAbs*70;
    const col=v>=0?'var(--g)':'var(--r)';
    html+=`<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end" title="${h}h: $${v.toFixed(1)}">`;
    html+=`<div style="width:80%;height:${hAbs}px;background:${col};opacity:.7;border-radius:1px"></div>`;
    html+='</div>';
  }
  html+='</div><div style="display:flex;justify-content:space-between;font-size:9px;color:var(--m);margin-top:2px"><span>0h</span><span>6h</span><span>12h</span><span>18h</span><span>23h</span></div>';
  html+='</div>';
  const sp=data.strat_pnl||{};
  html+='<div class="card"><h3>전략별 7일 PnL</h3>';
  for(const k of Object.keys(sp)){
    const v=sp[k]||0;
    const col=v>=0?'var(--g)':'var(--r)';
    html+=`<div style="display:flex;padding:4px 0;border-bottom:1px solid var(--d);font-size:11px;align-items:center">
      <span style="width:60px;font-weight:600">${k}</span>
      <span style="flex:1"></span>
      <span style="font-family:'SF Mono',monospace;color:${col}">${v>0?'+':''}$${v.toFixed(1)}</span>
    </div>`;
  }
  html+='</div>';
  $('content').innerHTML=html;
  drawBalChart();
}

function drawBalChart(){
  const el=$('bal-chart');
  if(!el || !window.LightweightCharts) return;
  if(balChart){ try{balChart.remove();}catch(e){} balChart=null; }
  const bh=data.bal_history||[];
  if(!bh.length){ el.innerHTML='<div style="padding:30px;text-align:center;color:var(--m);font-size:11px">잔고 데이터 없음</div>'; return; }
  balChart=LightweightCharts.createChart(el,{
    width:el.clientWidth, height:180,
    layout:{background:{color:'#0a0e17'},textColor:'#64748b'},
    grid:{vertLines:{color:'#1e293b'},horzLines:{color:'#1e293b'}},
    timeScale:{timeVisible:true,secondsVisible:false,borderColor:'#334155'},
    rightPriceScale:{borderColor:'#334155'},
    handleScroll:false, handleScale:false,
  });
  balSeries=balChart.addAreaSeries({
    lineColor:'#3b82f6', topColor:'rgba(59,130,246,0.4)', bottomColor:'rgba(59,130,246,0.0)',
    lineWidth:2,
  });
  const yr=new Date().getUTCFullYear();
  // bh format: [{t:"MM-DD HH:MM", b:3260}, ...] — UTC 가정 (V10.31AK 통일)
  const cdata=bh.map(p=>{
    try{
      const [md, hm] = p.t.split(' ');
      const [m,d] = md.split('-').map(Number);
      const [hh,mm] = hm.split(':').map(Number);
      const ts = Date.UTC(yr, m-1, d, hh, mm) / 1000;
      return {time: ts, value: p.b};
    }catch(e){ return null; }
  }).filter(x=>x && !isNaN(x.value));
  // ★ time 오름차순 + 중복 ts 제거 (lightweight-charts 요구사항)
  cdata.sort((a,b)=>a.time-b.time);
  const dedup=[];
  let lastT=-1;
  for(const p of cdata){
    if(p.time !== lastT){
      dedup.push(p);
      lastT=p.time;
    }
  }
  balSeries.setData(dedup);
  balChart.timeScale().fitContent();
}

// ─── 인사이트 탭 ────────────────────────────────────────────────────
function renderInsight(){
  const ins=data.insights||[];
  const perf=data.perf||{};
  const costs=data.costs_7d||{};
  let html='';
  html+='<div class="card"><h3>성과 지표 <span class="sub">'+(perf.n_days||0)+'일</span></h3>';
  html+=`<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:11px">
    <div style="padding:8px;background:#1e293b;border-radius:4px">
      <div style="color:var(--m);font-size:10px">누적 수익률</div>
      <div style="font-size:16px;font-weight:600;color:${(perf.total_return_pct||0)>=0?'var(--g)':'var(--r)'};font-family:'SF Mono',monospace">${(perf.total_return_pct||0).toFixed(2)}%</div>
    </div>
    <div style="padding:8px;background:#1e293b;border-radius:4px">
      <div style="color:var(--m);font-size:10px">MDD</div>
      <div style="font-size:16px;font-weight:600;color:var(--r);font-family:'SF Mono',monospace">${(perf.mdd_pct||0).toFixed(2)}%</div>
    </div>
    <div style="padding:8px;background:#1e293b;border-radius:4px">
      <div style="color:var(--m);font-size:10px">Sharpe</div>
      <div style="font-size:16px;font-weight:600;font-family:'SF Mono',monospace">${(perf.sharpe||0).toFixed(2)}</div>
    </div>
    <div style="padding:8px;background:#1e293b;border-radius:4px">
      <div style="color:var(--m);font-size:10px">7d 비용</div>
      <div style="font-size:13px;font-family:'SF Mono',monospace;line-height:1.3">
        수수료 <span style="color:var(--r)">$${Math.abs(costs.fee||0).toFixed(2)}</span><br>
        펀딩 <span style="color:${(costs.funding||0)>=0?'var(--g)':'var(--r)'}">${(costs.funding||0)>=0?'+':''}$${(costs.funding||0).toFixed(2)}</span>
      </div>
    </div>
  </div>`;
  if(perf.warning) html+=`<div style="margin-top:8px;padding:6px 10px;background:#1e293b;border-left:3px solid var(--a);border-radius:4px;font-size:11px;color:var(--m)">⚠️ ${perf.warning}</div>`;
  html+='</div>';
  html+='<div class="card"><h3>현재 상태 인사이트</h3>';
  if(!ins.length){
    html+='<div style="color:var(--m);font-size:12px;padding:8px">특이사항 없음</div>';
  } else {
    const iconMap = {crit:'🚨', warn:'⚠️', info:'📊', good:'✅'};
    const colorMap = {crit:'var(--r)', warn:'var(--a)', info:'var(--c)', good:'var(--g)'};
    for(const it of ins){
      const lv = it.level || 'info';
      html+=`<div style="padding:8px 10px;margin-bottom:6px;background:#1e293b;border-left:3px solid ${colorMap[lv]};border-radius:4px;font-size:12px;display:flex;gap:8px;align-items:flex-start">
        <span>${iconMap[lv]}</span><span style="flex:1;color:var(--t);line-height:1.4">${it.text}</span>
      </div>`;
    }
  }
  html+='</div>';
  const ss=data.symbol_stats||[];
  if(ss.length){
    html+='<div class="card"><h3>심볼별 실적 <span class="sub">7일</span></h3>';
    html+=`<div style="display:grid;grid-template-columns:60px 30px 60px 50px 50px 30px;gap:4px;padding:4px;color:var(--m);border-bottom:1px solid var(--d);font-weight:600;font-size:10px">
      <span>심볼</span><span>n</span><span>PnL</span><span>건당</span><span>WR</span><span></span>
    </div>`;
    for(const s of ss){
      const pc=s.pnl>=0?'var(--g)':'var(--r)';
      const cd=s.cooldown?'<span style="color:var(--r);font-size:9px">CD</span>':'';
      html+=`<div style="display:grid;grid-template-columns:60px 30px 60px 50px 50px 30px;gap:4px;padding:4px;border-bottom:1px solid var(--d);font-family:'SF Mono',monospace;align-items:center;font-size:11px">
        <span style="color:${s.cooldown?'var(--m)':'var(--t)'}">${s.sym}</span>
        <span>${s.n}</span>
        <span style="color:${pc}">${s.pnl>0?'+':''}$${s.pnl.toFixed(1)}</span>
        <span style="color:${pc}">${s.avg>0?'+':''}$${s.avg.toFixed(2)}</span>
        <span>${s.wr}%</span>
        <span>${cd}</span>
      </div>`;
    }
    html+='</div>';
  }
  $('content').innerHTML=html;
}

// 모달 배경 클릭 시 닫기
document.getElementById('modal').addEventListener('click', e => {
  if(e.target.id==='modal') closeModal();
});

fetchData();
setInterval(fetchData, 3000);
</script>
</body>
</html>"""


# ─── HTTP 핸들러 ───────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            qs = urllib.parse.parse_qs(parsed.query)

            if path == '/api/status':
                self._serve_file_json(STATUS_FILE)
            elif path == '/api/chart':
                sym = (qs.get('sym') or [''])[0].upper().replace('/USDT', '')
                if not sym:
                    self._send_json({"error": "sym required"}, 400)
                    return
                self._send_json(fetch_chart_for_sym(sym))
            elif path == '/api/markers':
                sym = (qs.get('sym') or [''])[0].upper().replace('/USDT', '')
                if not sym:
                    self._send_json({"error": "sym required"}, 400)
                    return
                self._send_json({"markers": extract_markers(sym, 7)})
            elif path == '/api/closed_syms':
                # 활성 심볼 추출
                active = set()
                try:
                    if os.path.exists(STATUS_FILE):
                        with open(STATUS_FILE, 'r') as f:
                            sd = json.load(f)
                        for p in sd.get('positions', []):
                            if p.get('sym'):
                                active.add(p['sym'])
                except Exception:
                    pass
                self._send_json(get_closed_symbols(7, exclude=active))
            else:
                self._serve_dashboard()
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def _serve_file_json(self, path):
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    data = f.read()
            else:
                data = json.dumps({"error": "not ready", "ts": time.time()})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data.encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _send_json(self, data, status=200):
        try:
            body = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            try:
                self.send_error(500, str(e))
            except Exception:
                pass

    def _serve_dashboard(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    print(f"[DASHBOARD] http://0.0.0.0:{PORT}")
    # ★ ThreadingHTTPServer — ccxt lazy fetch가 다른 polling 요청을 블록하지 않도록
    server = http.server.ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[DASHBOARD] 종료")
