#!/usr/bin/env node
// Synthetic 15-min intraday backtest with ICT/SMC-like confirmation filters
const fs = require('fs');
const path = require('path');
const YahooFinance = require('yahoo-finance2').default;
const yf = new YahooFinance({ suppressNotices: ['ripHistorical'] });

// Black-Scholes
function normPdf(x){ return Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI); }
function normCdf(x){ const k=1/(1+0.2316419*Math.abs(x)); const a1=0.31938153,a2=-0.356563782,a3=1.781477937,a4=-1.821255978,a5=1.330274429; const poly = (((a5*k+a4)*k+a3)*k+a2)*k+a1; const approx = 1 - normPdf(x)*poly*k; return x>=0? approx : 1-approx; }
function bsPrice(S,K,r,sigma,t,otype){ if(t<=0) return Math.max(otype==='call'?S-K:K-S,0); if(sigma<=0) return Math.max(otype==='call'?S-K*Math.exp(-r*t):K*Math.exp(-r*t)-S,0); const d1=(Math.log(S/K)+(r+0.5*sigma*sigma)*t)/(sigma*Math.sqrt(t)); const d2=d1-sigma*Math.sqrt(t); if(otype==='call') return S*normCdf(d1)-K*Math.exp(-r*t)*normCdf(d2); return K*Math.exp(-r*t)*normCdf(-d2)-S*normCdf(-d1); }

// Parameters
const SYMBOL='SPY';
const START=new Date('2025-01-20T00:00:00Z');
const END=new Date('2026-05-08T23:59:59Z');
const ALLOCATION=1000; // per trade
const MAX_LOSS=0.20*ALLOCATION; // $200
const RFR=0.05;
const OTM_PCT=0.015; // 1.5% OTM

const MARKET_OPEN_HOUR = 13; // 13:30 UTC ~ 9:30 ET
const MARKET_OPEN_MIN = 30;
const BAR_MIN = 15; // 15-minute bars
const BARS_PER_DAY = Math.floor(390 / BAR_MIN); // 26
const ENTRY_WINDOW_MINUTES = 90; // only first 90 minutes
const ENTRY_WINDOW_BARS = Math.ceil(ENTRY_WINDOW_MINUTES / BAR_MIN); // 6

// helpers
function isWeekday(d){ const wd=d.getUTCDay(); return wd!==0 && wd!==6; }
function nextFridayUTC(d){ const r=new Date(d); let wd=r.getUTCDay(); const daysUntil=(5-wd+7)%7||7; r.setUTCDate(r.getUTCDate()+daysUntil); r.setUTCHours(0,0,0,0); return r; }
function round2(x){ return Math.round(x*100)/100; }

async function fetchDaily(sym,start,end){
  try{ const data = await yf.historical(sym, { period1: start.toISOString().slice(0,10), period2: end.toISOString().slice(0,10) }); if(Array.isArray(data) && data.length>0) return data.map(d=>({ date:new Date(d.date), open:d.open, high:d.high, low:d.low, close:d.close, volume:d.volume })); }catch(e){}
  const data2 = await yf.chart(sym, { period1: start.toISOString().slice(0,10), period2: end.toISOString().slice(0,10), interval:'1d' });
  const res = data2 && data2.result && data2.result[0]; if(!res) throw new Error('No daily data'); const ts=res.timestamp||[]; const q=res.indicators && res.indicators.quote && res.indicators.quote[0]; const rows=[]; for(let i=0;i<ts.length;i++) rows.push({ date:new Date(ts[i]*1000), open:q.open[i], high:q.high[i], low:q.low[i], close:q.close[i], volume:q.volume[i] }); return rows;
}

function synthIntradayForDay(daily){
  const bars=[];
  // market open at 13:30 UTC
  const openTs = Date.UTC(daily.date.getUTCFullYear(), daily.date.getUTCMonth(), daily.date.getUTCDate(), MARKET_OPEN_HOUR, MARKET_OPEN_MIN, 0);
  const open = daily.open, high=daily.high, low=daily.low, close=daily.close;
  const peakIdx = Math.floor(BARS_PER_DAY*0.3);
  const troughIdx = Math.floor(BARS_PER_DAY*0.6);
  for(let i=0;i<BARS_PER_DAY;i++){
    let price;
    if(i<=peakIdx) price = open + (high-open) * (i/Math.max(1,peakIdx));
    else if(i<=troughIdx) price = high + (low-high) * ((i-peakIdx)/Math.max(1,troughIdx-peakIdx));
    else price = low + (close-low) * ((i-troughIdx)/Math.max(1,BARS_PER_DAY-troughIdx));
    const noise = (Math.random()-0.5) * (daily.high-daily.low) * 0.005; // small noise
    const ts = new Date(openTs + i * BAR_MIN * 60 * 1000);
    bars.push({ date: ts, open: price+noise, high: price+Math.abs(noise), low: price-Math.abs(noise), close: price+noise });
  }
  return bars;
}

function annVolFromCloses(closes, lookback=30){ if(closes.length<2) return 0.3; const arr=closes.slice(-Math.min(closes.length, lookback+1)); const rets=[]; for(let i=1;i<arr.length;i++) rets.push(Math.log(arr[i]/arr[i-1])); const mean=rets.reduce((a,b)=>a+b,0)/rets.length; const varr=rets.reduce((a,b)=>a+(b-mean)*(b-mean),0)/(Math.max(1,rets.length-1)); const sd=Math.sqrt(varr); return sd*Math.sqrt(252); }

function detectBreakouts(bars, lookback=20){ const signals=[]; for(let i=lookback;i<bars.length;i++){ let high=-Infinity, low=Infinity; for(let j=i-lookback;j<i;j++){ if(bars[j].high>high) high=bars[j].high; if(bars[j].low<low) low=bars[j].low; } if(bars[i].close>high) signals.push({ idx:i, type:'long', level:high }); if(bars[i].close<low) signals.push({ idx:i, type:'short', level:low }); } return signals; }

function computeATR(bars, period=14){ const trs=[]; for(let i=1;i<bars.length;i++){ const cur=bars[i], prev=bars[i-1]; const tr = Math.max(cur.high-cur.low, Math.abs(cur.high-prev.close), Math.abs(cur.low-prev.close)); trs.push(tr); } if(trs.length<period) return null; let sum=0; for(let i=trs.length-period;i<trs.length;i++) sum+=trs[i]; return sum/period; }

async function run(){
  console.log('Fetching daily SPY...');
  const daily = await fetchDaily(SYMBOL, START, END);
  const dailyCloses = daily.map(d=>d.close);
  const bars=[];
  for(const d of daily){ if(!isWeekday(d.date)) continue; bars.push(...synthIntradayForDay(d)); }
  console.log('Synth bars:', bars.length);

  const signals = detectBreakouts(bars, 20);
  console.log('Raw signals:', signals.length);

  // run with filters
  const trades=[]; let lastTradeDay=null;
  for(const s of signals){
    const entryIdx = s.idx; const entry = bars[entryIdx];
    // time-of-day filter: only in first ENTRY_WINDOW_BARS
    const dayStart = new Date(Date.UTC(entry.date.getUTCFullYear(), entry.date.getUTCMonth(), entry.date.getUTCDate(), MARKET_OPEN_HOUR, MARKET_OPEN_MIN, 0));
    const minutesFromOpen = (entry.date - dayStart)/(60*1000);
    const barFromOpen = Math.floor(minutesFromOpen/BAR_MIN);
    if(barFromOpen >= ENTRY_WINDOW_BARS) continue;
    // one trade per day
    const tradeDayKey = `${entry.date.getUTCFullYear()}-${entry.date.getUTCMonth()}-${entry.date.getUTCDate()}`;
    if(lastTradeDay === tradeDayKey) continue;
    // ATR filter on prior bars of same day
    const dayBarsStart = entryIdx - barFromOpen;
    const recentBars = bars.slice(Math.max(0, dayBarsStart-30), entryIdx+1);
    const atr = computeATR(recentBars, 14);
    if(atr === null) continue;
    const atrPct = atr / entry.close;
    if(atrPct < 0.0015) continue; // require min volatility ~0.15%
    // confirmation: require two-bar close confirmation above/below level
    const confirmIdx = entryIdx + 1;
    const confirmIdx2 = entryIdx + 2;
    if(confirmIdx2 >= bars.length) continue;
    const c1 = bars[confirmIdx], c2 = bars[confirmIdx2];
    if(s.type==='long'){ if(!(c1.close > s.level && c2.close > s.level)) continue; }
    else { if(!(c1.close < s.level && c2.close < s.level)) continue; }

    // build option trade
    const S = entry.close; const strike = round2(s.type==='long'? S*(1+OTM_PCT) : S*(1-OTM_PCT));
    const expiry = nextFridayUTC(entry.date); const daysToExp = Math.max(1, Math.ceil((expiry - entry.date)/(24*3600*1000))); const t = daysToExp/365;
    const iv = annVolFromCloses(dailyCloses, 30);
    const optType = s.type==='long' ? 'call' : 'put';
    const premium = bsPrice(S, strike, RFR, iv, t, optType);
    if(!premium || premium<=0) continue;
    const contracts = Math.floor(ALLOCATION / (premium*100)); if(contracts<=0) continue;
    const entryCost = premium * contracts * 100;
    // simulate forward until stop or expiry but only within same day or until expiry
    let exit=null; let open=true;
    for(let j=entryIdx+1;j<bars.length;j++){
      const r = bars[j];
      // block entries later same day
      // if past expiry
      if(r.date >= expiry){ const intrinsic = Math.max(optType==='call'? r.close - strike : strike - r.close, 0); const val = intrinsic*contracts*100; const pnl = val - entryCost; exit={ date: r.date.toISOString(), pnl }; open=false; break; }
      // allow exit during same day if loss hits
      const t_rem = Math.max(1/365, (expiry - r.date)/(365*24*3600*1000)); const mark = bsPrice(r.close, strike, RFR, iv, t_rem, optType); const val = mark*contracts*100; const pnl = val - entryCost;
      if(pnl <= -MAX_LOSS){ exit={ date: r.date.toISOString(), pnl }; open=false; break; }
      // optionally take profit (e.g., 100% gain)
      if(pnl >= entryCost * 1.0){ exit={ date: r.date.toISOString(), pnl }; open=false; break; }
      // stop scanning beyond same day if market closed
      const sameDayKey = `${r.date.getUTCFullYear()}-${r.date.getUTCMonth()}-${r.date.getUTCDate()}`;
      if(sameDayKey !== tradeDayKey) { /* continue to expiry though */ }
    }
    if(open){ const last = bars[bars.length-1]; const t_rem = Math.max(1/365, (expiry - last.date)/(365*24*3600*1000)); const mark = bsPrice(last.close, strike, RFR, iv, t_rem, optType); const val = mark*contracts*100; const pnl = val - entryCost; exit={ date: last.date.toISOString(), pnl }; }

    trades.push({ entryDate: entry.date.toISOString(), type: optType, strike, entryUnderlying: S, premium, contracts, entryCost, exit });
    lastTradeDay = tradeDayKey;
  }

  // summarize
  let wins=0, losses=0, tot=0; let cum=5000, peak=5000, maxDraw=0; const equity=[5000];
  for(const t of trades){ const p = t.exit && typeof t.exit.pnl==='number' ? t.exit.pnl : 0; cum += p; if(cum>peak) peak=cum; const draw = peak - cum; if(draw>maxDraw) maxDraw=draw; if(p>0) wins++; else losses++; tot+=p; equity.push(cum); }

  const out = { generated: new Date().toISOString(), period:{start:START.toISOString(), end:END.toISOString()}, trades: trades.length, wins, losses, winRate: trades.length? (wins/trades.length):0, totalPnl: tot, maxDrawdown: maxDraw };
  const outDir = path.resolve('backtest-output'); if(!fs.existsSync(outDir)) fs.mkdirSync(outDir);
  fs.writeFileSync(path.join(outDir,'sim_15m_trades.json'), JSON.stringify(trades,null,2));
  fs.writeFileSync(path.join(outDir,'sim_15m_summary.json'), JSON.stringify(out,null,2));
  console.log('Done. Trades:', trades.length, 'Win rate:', out.winRate.toFixed(3), 'Total PnL:', out.totalPnl);
}

run().catch(e=>{ console.error('Fail', e && e.message? e.message : e); process.exit(1); });

