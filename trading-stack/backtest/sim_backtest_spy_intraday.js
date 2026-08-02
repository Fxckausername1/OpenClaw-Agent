#!/usr/bin/env node
// Synthetic intraday backtest for SPY options using daily data -> 5m bars
const fs = require('fs');
const path = require('path');
const YahooFinance = require('yahoo-finance2').default;
const yf = new YahooFinance({ suppressNotices: ['ripHistorical'] });

// Black-Scholes
function normPdf(x){ return Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI); }
function normCdf(x){ const k=1/(1+0.2316419*Math.abs(x)); const a1=0.31938153,a2=-0.356563782,a3=1.781477937,a4=-1.821255978,a5=1.330274429; const poly = (((a5*k+a4)*k+a3)*k+a2)*k+a1; const approx = 1 - normPdf(x)*poly*k; return x>=0? approx : 1-approx; }
function bsPrice(S,K,r,sigma,t,otype){ if(t<=0) return Math.max(otype==='call'?S-K:K-S,0); if(sigma<=0) return Math.max(otype==='call'?S-K*Math.exp(-r*t):K*Math.exp(-r*t)-S,0); const d1=(Math.log(S/K)+(r+0.5*sigma*sigma)*t)/(sigma*Math.sqrt(t)); const d2=d1-sigma*Math.sqrt(t); if(otype==='call') return S*normCdf(d1)-K*Math.exp(-r*t)*normCdf(d2); return K*Math.exp(-r*t)*normCdf(-d2)-S*normCdf(-d1); }

// Parameters
const SYMBOL = 'SPY';
const START = new Date('2025-01-20T00:00:00Z');
const END = new Date('2026-05-08T23:59:59Z');
const ALLOCATION = 1000; // per trade
const MAX_LOSS = 0.20 * ALLOCATION; // $200
const RFR = 0.05;
const OTM_PCT = 0.015; // 1.5% OTM

const MARKET_MINUTES = 390; // 6.5 hours
const BAR_MIN = 5; // 5-minute bars
const BARS_PER_DAY = Math.floor(MARKET_MINUTES / BAR_MIN); // 78

// Helpers
function addDays(d, days){ const r=new Date(d); r.setUTCDate(r.getUTCDate()+days); return r; }
function isWeekday(d){ const wd=d.getUTCDay(); return wd!==0 && wd!==6; }
function nextFridayUTC(d){ const r=new Date(d); let wd=r.getUTCDay(); const daysUntil = (5 - wd + 7) % 7 || 7; r.setUTCDate(r.getUTCDate()+daysUntil); r.setUTCHours(0,0,0,0); return r; }

async function fetchDaily(sym, start, end){
  // Try historical endpoint first
  try{
    const opts = { period1: start.toISOString().slice(0,10), period2: end.toISOString().slice(0,10) };
    const data = await yf.historical(sym, opts);
    if(Array.isArray(data) && data.length>0){ return data.map(d=>({ date: new Date(d.date), open: d.open, high: d.high, low: d.low, close: d.close, volume: d.volume })); }
  }catch(e){ /* fallthrough to chart */ }
  // fallback to chart()
  const opts2 = { period1: start.toISOString().slice(0,10), period2: end.toISOString().slice(0,10), interval: '1d' };
  const data2 = await yf.chart(sym, opts2);
  const res = data2 && data2.result && data2.result[0];
  if(!res) throw new Error('No daily data');
  const ts = res.timestamp || [];
  const q = res.indicators && res.indicators.quote && res.indicators.quote[0];
  const rows=[];
  for(let i=0;i<ts.length;i++) rows.push({ date: new Date(ts[i]*1000), open: q.open[i], high: q.high[i], low: q.low[i], close: q.close[i], volume: q.volume[i] });
  return rows;
}

function synthIntradayForDay(daily){
  // create BARS_PER_DAY bars that start at open, go to high, then low, then close
  const bars = [];
  const open = daily.open, high = daily.high, low = daily.low, close = daily.close;
  // choose peak and trough positions
  const peakIdx = Math.floor(BARS_PER_DAY * 0.3); // early-ish
  const troughIdx = Math.floor(BARS_PER_DAY * 0.6);
  for(let i=0;i<BARS_PER_DAY;i++){
    let price;
    if(i<=peakIdx){ price = open + (high - open) * (i/Math.max(1,peakIdx)); }
    else if(i<=troughIdx){ price = high + (low - high) * ((i-peakIdx)/Math.max(1,troughIdx-peakIdx)); }
    else { price = low + (close - low) * ((i-troughIdx)/Math.max(1,BARS_PER_DAY-troughIdx)); }
    // add small random noise
    const noise = (Math.random()-0.5) * (daily.high - daily.low) * 0.01;
    const ts = new Date(daily.date.getTime() + i * BAR_MIN * 60 * 1000);
    bars.push({ date: ts, open: price + noise, high: price + Math.abs(noise), low: price - Math.abs(noise), close: price + noise });
  }
  return bars;
}

function annualizedVolFromCloses(closes, lookback=30){ if(closes.length<2) return 0.3; const arr=closes.slice(-Math.min(closes.length, lookback+1)); const rets=[]; for(let i=1;i<arr.length;i++) rets.push(Math.log(arr[i]/arr[i-1])); const mean = rets.reduce((a,b)=>a+b,0)/rets.length; const varr = rets.reduce((a,b)=>a+(b-mean)*(b-mean),0)/(Math.max(1,rets.length-1)); const sd=Math.sqrt(varr); return sd*Math.sqrt(252); }

function detectBreakouts(bars, lookback=20){ const signals=[]; for(let i=lookback;i<bars.length;i++){ let high=-Infinity, low=Infinity; for(let j=i-lookback;j<i;j++){ if(bars[j].high>high) high=bars[j].high; if(bars[j].low<low) low=bars[j].low; } if(bars[i].close>high) signals.push({ idx:i, type:'long' }); if(bars[i].close<low) signals.push({ idx:i, type:'short' }); } return signals; }

async function run(){
  console.log('Fetching daily SPY from', START.toISOString().slice(0,10), 'to', END.toISOString().slice(0,10));
  const daily = await fetchDaily(SYMBOL, START, END);
  const dailyCloses = daily.map(r=>r.close);
  const bars = [];
  for(const d of daily){ if(!isWeekday(d.date)) continue; const intr = synthIntradayForDay(d); bars.push(...intr); }
  console.log('Synthesized bars:', bars.length);

  const signals = detectBreakouts(bars, 20);
  console.log('Signals:', signals.length);

  const trades = [];
  for(const s of signals){
    const entryIdx = s.idx; const entry = bars[entryIdx]; const S = entry.close;
    const strike = Math.round((s.type==='long'? S*(1+OTM_PCT) : S*(1-OTM_PCT)) * 100) / 100;
    const expiry = nextFridayUTC(entry.date);
    const daysToExp = Math.max(1, Math.ceil((expiry - entry.date)/(24*3600*1000)));
    const t = daysToExp/365;
    const optType = s.type==='long'?'call':'put';
    const iv = annualizedVolFromCloses(dailyCloses, 30);
    const premium = bsPrice(S, strike, RFR, iv, t, optType);
    if(!premium || premium<=0) continue;
    const contracts = Math.floor(ALLOCATION / (premium*100));
    if(contracts<=0) continue;
    const entryCost = premium * contracts * 100;
    let exit=null; let open=true;
    for(let j=entryIdx+1;j<bars.length;j++){
      const r = bars[j];
      if(r.date >= expiry){ const intrinsic = Math.max(optType==='call'? r.close - strike : strike - r.close, 0); const val = intrinsic*contracts*100; const pnl = val - entryCost; exit={ date: r.date.toISOString(), pnl }; open=false; break; }
      const t_rem = Math.max(1/365, (expiry - r.date)/(365*24*3600*1000));
      const mark = bsPrice(r.close, strike, RFR, iv, t_rem, optType);
      const val = mark*contracts*100; const pnl = val - entryCost;
      if(pnl <= -MAX_LOSS){ exit={ date: r.date.toISOString(), pnl }; open=false; break; }
    }
    if(open){ const last = bars[bars.length-1]; const t_rem = Math.max(1/365, (expiry - last.date)/(365*24*3600*1000)); const mark = bsPrice(last.close, strike, RFR, iv, t_rem, optType); const val = mark*contracts*100; const pnl = val - entryCost; exit={ date: last.date.toISOString(), pnl }; }
    trades.push({ entryDate: entry.date.toISOString(), type: optType, strike, entryUnderlying: S, premium, contracts, entryCost, exit });
  }

  // summarize
  let wins=0, losses=0, tot=0; const equity=[5000];
  for(const t of trades){ const p = t.exit?pnlVal(t.exit.pnl):0; /* placeholder */ }
  // compute correctly
  let cum=5000; let maxDraw=0, peak=5000;
  for(const t of trades){ const p = t.exit && typeof t.exit.pnl==='number' ? t.exit.pnl : 0; cum += p; if(cum>peak) peak=cum; const draw = peak - cum; if(draw>maxDraw) maxDraw=draw; if(p>0) wins++; else losses++; tot+=p; equity.push(cum); }

  const out = { generated: new Date().toISOString(), period: { start: START.toISOString(), end: END.toISOString() }, trades: trades.length, wins, losses, winRate: trades.length? (wins/trades.length):0, totalPnl: tot, maxDrawdown: maxDraw };
  const outDir = path.resolve('backtest-output'); if(!fs.existsSync(outDir)) fs.mkdirSync(outDir);
  fs.writeFileSync(path.join(outDir,'sim_trades.json'), JSON.stringify(trades,null,2));
  fs.writeFileSync(path.join(outDir,'sim_summary.json'), JSON.stringify(out,null,2));
  console.log('Done. Trades:', trades.length, 'Win rate:', out.winRate.toFixed(3));
}

function pnlVal(v){ return typeof v==='number'? v : 0; }

run().catch(e=>{ console.error('Fail', e && e.message? e.message : e); process.exit(1); });
