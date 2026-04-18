"""
★ V10.29e: Trinity 라이브 대시보드 서버
표준 라이브러리만 사용 (외부 의존성 없음)

사용법:
  python3 status_server.py [port]     (기본 포트: 7777)

EC2 보안그룹에서 해당 포트 열어야 외부 접근 가능.
"""
import http.server
import json
import os
import sys
import time

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 7777
STATUS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v9_status.json")

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>Trinity Live</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0a0e17;--card:#111827;--border:#1e293b;--g:#22c55e;--r:#ef4444;--a:#f59e0b;--b:#3b82f6;--c:#06b6d4;--t:#e2e8f0;--m:#64748b;--d:#334155}
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
.card h3{font-size:12px;font-weight:600;margin-bottom:8px;color:var(--t)}
.pos{display:flex;align-items:center;gap:6px;margin-bottom:5px;font-size:12px}
.pos .sym{width:42px;font-weight:600;font-family:'SF Mono',monospace}
.pos .badge{font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600;text-align:center}
.pos .side-l{background:#16a34a22;color:var(--g)}
.pos .side-s{background:#dc262622;color:var(--r)}
.pos .t1{background:#06b6d422;color:var(--c)}
.pos .t2{background:#f59e0b22;color:var(--a)}
.pos .t3{background:#ef444422;color:var(--r)}
.pos .bar-wrap{flex:1;height:14px;background:var(--d);border-radius:4px;overflow:hidden;position:relative}
.pos .bar{height:100%;border-radius:4px;transition:width .5s}
.pos .roi{width:50px;text-align:right;font-weight:600;font-family:'SF Mono',monospace;font-size:11px}
.trades{max-height:280px;overflow-y:auto}
.trade{display:flex;gap:6px;padding:4px 0;border-bottom:1px solid var(--d);font-size:11px;align-items:center}
.trade:last-child{border:none}
.trade .tt{color:var(--m);width:80px;font-size:10px}
.trade .ts2{font-weight:600;width:40px;font-family:'SF Mono',monospace}
.trade .tr{flex:1;font-size:10px;color:var(--m)}
.trade .tp{font-weight:600;font-family:'SF Mono',monospace;width:50px;text-align:right}
.skew-bar{height:20px;background:var(--d);border-radius:10px;position:relative;overflow:hidden;margin-top:6px}
.skew-fill{height:100%;border-radius:10px;transition:width .5s}
.skew-label{position:absolute;top:50%;transform:translateY(-50%);font-size:10px;font-weight:600;padding:0 8px}
.tab-bar{display:flex;gap:4px;margin-bottom:10px}
.tab{flex:1;text-align:center;padding:7px;font-size:11px;font-weight:600;border:1px solid var(--d);border-radius:6px;cursor:pointer;color:var(--m);background:transparent;text-transform:uppercase}
.tab.active{background:var(--b);color:#fff;border-color:var(--b)}
.err{text-align:center;padding:40px;color:var(--r);font-size:13px}
.stale{color:var(--a) !important}
.refresh-note{text-align:center;font-size:9px;color:var(--d);margin-top:8px}
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

<script>
let currentTab='pos', data=null;

function setTab(t){
  currentTab=t;
  document.querySelectorAll('.tab').forEach(e=>e.classList.remove('active'));
  document.getElementById('tab-'+t).classList.add('active');
  render();
}

function $(id){return document.getElementById(id)}

function c(v,pos){return v>0?(pos||'var(--g)'):(v<0?'var(--r)':'var(--m)')}

async function fetchData(){
  try{
    const r=await fetch('/api/status?t='+Date.now());
    if(!r.ok) throw new Error(r.status);
    data=await r.json();
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

  // Header
  $('ts').textContent=data.time.slice(11,19)+' UTC';
  $('ts').className='ts'+(stale?' stale':'');
  $('dot').style.background=stale?'var(--a)':'var(--g)';
  $('dot').style.boxShadow='0 0 8px '+(stale?'var(--a)':'var(--g)');

  // KPIs
  const s=data.summary, td=data.today;
  $('kpis').innerHTML=`
    <div class="kpi"><div class="l">Balance</div><div class="v">$${data.balance.toLocaleString()}</div><div class="s">MR ${(data.margin_ratio*100).toFixed(1)}%</div></div>
    <div class="kpi"><div class="l">미실현</div><div class="v" style="color:${c(s.unrealized_pnl)}">${s.unrealized_pnl>0?'+':''}$${s.unrealized_pnl.toFixed(1)}</div><div class="s">${s.total_positions}포지션 (L${s.long} S${s.short})</div></div>
    <div class="kpi"><div class="l">오늘 PnL</div><div class="v" style="color:${c(td.pnl)}">${td.pnl>0?'+':''}$${td.pnl.toFixed(1)}</div><div class="s">${td.trades}건 WR${td.wr}%</div></div>
    <div class="kpi"><div class="l">Urgency</div><div class="v" style="color:${data.skew.urgency>30?'var(--r)':data.skew.urgency>15?'var(--a)':'var(--c)'}">${data.skew.urgency||0}</div><div class="s">${data.skew.heavy_side||'-'} ${data.skew.heavy_roi||0}%</div></div>
  `;

  // Tab content
  if(currentTab==='pos') renderPositions();
  else if(currentTab==='trades') renderTrades();
  else if(currentTab==='today') renderToday();
  else if(currentTab==='insight') renderInsight();
  else renderSkew();
}

function renderPositions(){
  if(!data.positions.length){
    $('content').innerHTML='<div class="card"><h3>오픈 포지션 없음</h3></div>';
    return;
  }
  // ★ V10.31c: 코어 / 보조(BC/CB) 분리
  const corePos = data.positions.filter(p=>p.is_core!==false);
  const subPos  = data.positions.filter(p=>p.is_core===false);

  function renderRow(p){
    const isNeg=p.roi<0;
    const w=Math.min(Math.abs(p.roi)*10,100);
    const tc={1:'t1',2:'t2',3:'t3'}[p.tier]||'t1';
    const sc=p.side==='LONG'?'side-l':'side-s';
    const stepIcon=p.step>=1?'🔄':'';
    const roleBadge=p.role==='BC'?' 🌀':p.role==='CB'?' ⚡':'';
    return `<div class="pos">
      <span class="sym">${p.sym}${roleBadge}</span>
      <span class="badge ${sc}">${p.side}</span>
      <span class="badge ${tc}">T${p.tier}</span>
      <div class="bar-wrap"><div class="bar" style="width:${w}%;background:${isNeg?'var(--r)':'var(--g)'};opacity:.7"></div></div>
      <span class="roi" style="color:${c(p.roi)}">${p.roi>0?'+':''}${p.roi.toFixed(1)}%${stepIcon}</span>
    </div>`;
  }

  let html='<div class="card"><h3>코어 포지션 ('+corePos.length+')</h3>';
  if(corePos.length){
    for(const p of corePos) html+=renderRow(p);
  } else {
    html+='<div style="color:var(--m);font-size:11px;padding:6px">없음</div>';
  }
  // Summary row (코어 + 보조 합산 — 기존 유지)
  const s=data.summary;
  html+=`<div style="margin-top:8px;padding:6px 10px;background:#1e293b;border-radius:6px;display:flex;justify-content:space-between;font-size:11px">
    <span style="color:var(--m)">L $${s.long_notional.toFixed(0)} / S $${s.short_notional.toFixed(0)}</span>
    <span style="color:${c(s.unrealized_pnl)};font-weight:700;font-family:'SF Mono',monospace">${s.unrealized_pnl>0?'+':''}$${s.unrealized_pnl.toFixed(1)}</span>
  </div></div>`;

  // ★ V10.31c: 보조 전략(BC/CB) 별도 섹션
  if(subPos.length){
    html+='<div class="card"><h3>보조 전략 🌀 BC / ⚡ CB ('+subPos.length+')</h3>';
    for(const p of subPos) html+=renderRow(p);
    const subPnl = subPos.reduce((a,p)=>a+p.pnl, 0);
    html+=`<div style="margin-top:8px;padding:6px 10px;background:#1e293b;border-radius:6px;text-align:right;font-size:11px">
      <span style="color:${c(subPnl)};font-weight:700;font-family:'SF Mono',monospace">미실현 ${subPnl>0?'+':''}$${subPnl.toFixed(1)}</span>
    </div></div>`;
  }

  $('content').innerHTML=html;
}

function renderTrades(){
  let html='<div class="card"><h3>최근 트레이드</h3><div class="trades">';
  if(!data.recent_trades.length){
    html+='<div style="color:var(--m);font-size:11px;padding:10px">기록 없음</div>';
  }
  for(const t of [...data.recent_trades].reverse()){
    const pc=c(t.pnl);
    const rc=t.reason==='FORCE_CLOSE'?'var(--r)':t.reason.includes('TRIM')?'var(--c)':'var(--m)';
    html+=`<div class="trade">
      <span class="tt">${t.time}</span>
      <span class="ts2">${t.sym}</span>
      <span class="tr" style="color:${rc}">${t.reason} T${t.tier}</span>
      <span class="tp" style="color:${pc}">${t.pnl>0?'+':''}$${t.pnl.toFixed(1)}</span>
    </div>`;
  }
  html+='</div></div>';
  $('content').innerHTML=html;
}

// ★ V10.31c: 오늘 탭 — 7일 일별 PnL + 전략별 기여 + 시간대별
function renderToday(){
  const daily = data.daily || [];
  const sp = data.strat_pnl || {};
  const hr = data.hour_pnl || [];

  // 일별 7일 막대 + 누적선
  let html='<div class="card"><h3>최근 7일 일별 PnL</h3>';
  if(!daily.length){
    html+='<div style="color:var(--m);font-size:11px;padding:10px">기록 없음</div>';
  } else {
    const maxAbs = Math.max(1, ...daily.map(d=>Math.abs(d.pnl)));
    let cum=0;
    for(const d of daily){
      cum += d.pnl;
      const w = Math.min(Math.abs(d.pnl)/maxAbs*100, 100);
      const bc = d.pnl>=0?'var(--g)':'var(--r)';
      html+=`<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;font-size:11px">
        <span style="width:42px;color:var(--m);font-family:'SF Mono',monospace">${d.date}</span>
        <div class="bar-wrap" style="flex:1"><div class="bar" style="width:${w}%;background:${bc};opacity:.7"></div></div>
        <span style="width:62px;text-align:right;color:${c(d.pnl)};font-family:'SF Mono',monospace">${d.pnl>0?'+':''}$${d.pnl.toFixed(1)}</span>
        <span style="width:44px;text-align:right;color:var(--m);font-size:10px">${d.wr}%</span>
      </div>`;
    }
    html+=`<div style="margin-top:8px;padding:6px 10px;background:#1e293b;border-radius:6px;display:flex;justify-content:space-between;font-size:11px">
      <span style="color:var(--m)">7일 누적</span>
      <span style="color:${c(cum)};font-weight:700;font-family:'SF Mono',monospace">${cum>0?'+':''}$${cum.toFixed(1)}</span>
    </div>`;
  }
  html+='</div>';

  // 전략별 기여도
  html+='<div class="card"><h3>전략별 기여도 (7일)</h3>';
  const strats=[['MR','코어 MR'],['TREND','🔀 TREND'],['BC','🌀 BC'],['CB','⚡ CB'],['OTHER','기타']];
  const total = Object.values(sp).reduce((a,b)=>a+b, 0);
  for(const [k,label] of strats){
    const v = sp[k] || 0;
    if(v===0) continue;
    const pct = total!==0 ? (v/Math.max(...Object.values(sp).map(Math.abs),1))*100 : 0;
    const w = Math.min(Math.abs(pct), 100);
    const bc = v>=0?'var(--g)':'var(--r)';
    html+=`<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px;font-size:11px">
      <span style="width:76px;color:var(--t)">${label}</span>
      <div class="bar-wrap" style="flex:1"><div class="bar" style="width:${w}%;background:${bc};opacity:.7"></div></div>
      <span style="width:62px;text-align:right;color:${c(v)};font-family:'SF Mono',monospace">${v>0?'+':''}$${v.toFixed(1)}</span>
    </div>`;
  }
  html+='</div>';

  // 시간대별 (오늘만, non-zero hour만)
  const nonZeroHours = hr.map((v,i)=>[i,v]).filter(([,v])=>v!==0);
  if(nonZeroHours.length){
    html+='<div class="card"><h3>오늘 시간대별 PnL (UTC)</h3>';
    const maxAbs = Math.max(1, ...nonZeroHours.map(([,v])=>Math.abs(v)));
    for(const [h,v] of nonZeroHours){
      const w = Math.min(Math.abs(v)/maxAbs*100, 100);
      const bc = v>=0?'var(--g)':'var(--r)';
      html+=`<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px;font-size:11px">
        <span style="width:32px;color:var(--m);font-family:'SF Mono',monospace">${String(h).padStart(2,'0')}h</span>
        <div class="bar-wrap" style="flex:1"><div class="bar" style="width:${w}%;background:${bc};opacity:.7"></div></div>
        <span style="width:58px;text-align:right;color:${c(v)};font-family:'SF Mono',monospace">${v>0?'+':''}$${v.toFixed(1)}</span>
      </div>`;
    }
    html+='</div>';
  }

  $('content').innerHTML=html;
}

// ★ V10.31c: 인사이트 탭 — 규칙 기반 경고/관찰
function renderInsight(){
  const ins = data.insights || [];
  let html='<div class="card"><h3>현재 상태 인사이트</h3>';
  if(!ins.length){
    html+='<div style="color:var(--m);font-size:12px;padding:10px">특이사항 없음 — 안정적 운영 중</div>';
  } else {
    const iconMap = {crit:'🚨', warn:'⚠️', info:'📊', good:'✅'};
    const colorMap = {crit:'var(--r)', warn:'var(--a)', info:'var(--c)', good:'var(--g)'};
    for(const it of ins){
      const lv = it.level || 'info';
      html+=`<div style="padding:8px 10px;margin-bottom:6px;background:#1e293b;border-left:3px solid ${colorMap[lv]};border-radius:4px;font-size:12px;display:flex;gap:8px;align-items:flex-start">
        <span>${iconMap[lv]}</span>
        <span style="flex:1;color:var(--t);line-height:1.4">${it.text}</span>
      </div>`;
    }
  }
  html+='</div>';
  html+='<div class="refresh-note" style="margin-top:8px">규칙 기반 자동 생성 · 3초마다 갱신</div>';
  $('content').innerHTML=html;
}

function renderSkew(){
  const sk=data.skew;
  const s=data.summary;
  const skewPct=Math.abs(sk.skew_pct||0);
  const urgPct=Math.min((sk.urgency||0)/60*100,100);

  let html='<div class="card"><h3>스큐 & 리스크</h3>';

  // Skew bar
  html+=`<div style="font-size:11px;color:var(--m);margin-bottom:4px">Skew ${(sk.skew_pct||0).toFixed(1)}% (${sk.heavy_side||'-'} 편중)</div>`;
  html+=`<div class="skew-bar"><div class="skew-fill" style="width:${Math.min(skewPct*2,100)}%;background:${skewPct>25?'var(--r)':skewPct>15?'var(--a)':'var(--g)'}"></div></div>`;

  // Urgency bar
  html+=`<div style="font-size:11px;color:var(--m);margin-top:10px;margin-bottom:4px">Urgency ${(sk.urgency||0).toFixed(0)}/60</div>`;
  html+=`<div class="skew-bar"><div class="skew-fill" style="width:${urgPct}%;background:${sk.urgency>30?'var(--r)':sk.urgency>15?'var(--a)':'var(--c)'}"></div></div>`;

  // Notional breakdown
  html+=`<div style="margin-top:12px;font-size:11px">
    <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--d)">
      <span style="color:var(--g)">LONG ×${s.long}</span>
      <span style="font-family:'SF Mono',monospace">$${s.long_notional.toFixed(0)}</span>
    </div>
    <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--d)">
      <span style="color:var(--r)">SHORT ×${s.short}</span>
      <span style="font-family:'SF Mono',monospace">$${s.short_notional.toFixed(0)}</span>
    </div>
    <div style="display:flex;justify-content:space-between;padding:4px 0">
      <span style="color:var(--m)">Margin Ratio</span>
      <span style="font-family:'SF Mono',monospace;color:${data.margin_ratio>0.8?'var(--r)':data.margin_ratio>0.6?'var(--a)':'var(--t)'}">${(data.margin_ratio*100).toFixed(1)}%</span>
    </div>
  </div>`;

  html+='</div>';

  // Position details
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

// Poll
fetchData();
setInterval(fetchData, 3000);
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/api/status'):
            self._serve_json()
        else:
            self._serve_dashboard()

    def _serve_json(self):
        try:
            if os.path.exists(STATUS_FILE):
                with open(STATUS_FILE, 'r', encoding='utf-8') as f:
                    data = f.read()
            else:
                data = json.dumps({"error": "status not ready", "ts": time.time()})
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data.encode('utf-8'))
        except Exception as e:
            self.send_error(500, str(e))

    def _serve_dashboard(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-cache')
        self.end_headers()
        self.wfile.write(DASHBOARD_HTML.encode('utf-8'))

    def log_message(self, fmt, *args):
        pass  # suppress access logs


if __name__ == '__main__':
    print(f"[DASHBOARD] http://0.0.0.0:{PORT}")
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[DASHBOARD] 종료")
