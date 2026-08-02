#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const YahooFinance = require('yahoo-finance2').default;
const yf = new YahooFinance({ suppressNotices: ['ripHistorical'] });
const { bsPrice } = require('../backtest/bs');

const SYMBOL = 'SPY';
const INTERVAL = '5m';
const DAYS = 7; // fetch last ~7 days (recent window for intraday)
const ALLOCATION = 1000; // per trade
const MAX_LOSS = 0.20 * ALLOCATION; // $200 per trade
const RISK_FREE = 0.05; // annual

function nextFriday(d){
  const day = d.getUTCDay(); // 0 Sun ... 5 Fri
  const daysUntilFri = (5 - day + 7) % 7 || 7;
  const res = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()+daysUntilFri));
  return res;
}

function roundStrike(x){ return Math.round(x*2)/2; }

async function fetch5m(sym, days){
  const to = new Date();
  const from = new Date(Date.now() - days*24*3600*1000);
  const opts = { period1: from.toISOString().slice(0,10), period2: to.toISOString().slice(0,10), interval: INTERVAL };
  const data = await yf.chart(sym, opts);
  const res = data.result && data.result[0];
  if(!res) throw new Error('No chart result');
  const timestamps = res.timestamp || [];
  const quote = res.indicators && res.indicators.quote && res.indicators.quote[0];
  const rows = [];
  for(let i=0;i<timestamps.length;i++){
    rows.push({ date: new Date(timestamps[i]*1000), open: quote.open[i], high: quote.high[i], low: quote.low[i], close: quote.close[i], volume: quote.volume[i] });
  }
  return rows;
}

async function fetchDaily(sym, days){
  const to = new Date();
  const from = new Date(Date.now() - days*24*3600*1000);
  const opts = { period1: from.toISOString().slice(0,10), period2: to.toISOString().slice(0,10), interval: '1d' };
  const data = await yf.chart(sym, opts);
  const res = data.result && data.result[0];
  const timestamps = res.timestamp || [];
  const quote = res.indicators && res.indicators.quote && res.indicators.quote[0];
  const closes = [];
  for(let i=0;i<timestamps.length;i++) closes.push(quote.close[i]);
  return closes;
}

function annualizedVolFromDaily(closes, lookbackDays=30){
  if(closes.length < lookbackDays+1) lookbackDays = Math.max(5, closes.length-1);
  const arr = closes.slice(- (lookbackDays+1));
  const rets = [];
  for(let i=1;i<arr.length;i++) rets.push(Math.log(arr[i]/arr[i-1]));
  const mean = rets.reduce((a,b)=>a+b,0)/rets.length;
  const variance = rets.reduce((a,b)=>a+(b-mean)*(b-mean),0)/(rets.length-1 || 1);
  const sd = Math.sqrt(variance);
  return sd * Math.sqrt(252);
}

function detectBreakouts(rows, lookback=20){
  // return array of indices where breakout up or down occurs relative to lookback highs/lows
  const signals = [];
  for(let i=lookback;i<rows.length;i++){
    let high= -Infinity, low= Infinity;
    for(let j=i-lookback;j<i;j++){ if(rows[j].high>high) high=rows[j].high; if(rows[j].low<low) low=rows[j].low; }
    if(rows[i].close > high) signals.push({ idx:i, type:'long', level: high });
    if(rows[i].close < low) signals.push({ idx:i, type:'short', level: low });
  }
  return signals;
}

async function run(){
  console.log('Fetching 5m data (this can take a minute)...');
  const rows = await fetch5m(SYMBOL, DAYS);
  const dailyCloses = await fetchDaily(SYMBOL, 60);
  const iv = annualizedVolFromDaily(dailyCloses, 30);
  console.log('Estimated IV (annual):', iv.toFixed(4));

  const signals = detectBreakouts(rows, 20);
  console.log('Detected signals:', signals.length);

  const trades = [];

  for(const s of signals){
    const entryIdx = s.idx;
    const entryRow = rows[entryIdx];
    const entryPrice = entryRow.close;
    const strikePct = entryPrice * (s.type==='long'? 1.01 : 0.99);
    const strike = roundStrike(strikePct);
    // expiry next Friday
    const expiry = nextFriday(entryRow.date);
    const daysToExp = Math.max(1, Math.ceil((expiry - entryRow.date)/(24*3600*1000)));
    const t = daysToExp/365;
    const optType = s.type==='long' ? 'call' : 'put';
    const premium = bsPrice(entryPrice, strike, RISK_FREE, iv, t, optType==='call'?'call':'put');
    if(!premium || premium<=0) continue;
    const contracts = Math.floor(ALLOCATION / (premium*100));
    if(contracts<=0) continue; // too expensive

    // simulate forward until stop loss or expiry
    let positionOpen = true;
    let entryCost = premium * contracts * 100;
    let maxLoss = MAX_LOSS;
    let exitInfo = null;
    for(let j=entryIdx+1;j<rows.length;j++){
      const r = rows[j];
      // if past expiry date, close at intrinsic value
      if(r.date >= expiry){
        const t2 = 0; // at expiry
        const priceAt = Math.max(optType==='call'? r.close - strike : strike - r.close, 0);
        const val = priceAt * contracts * 100;
        const pnl = val - entryCost;
        exitInfo = { exitDate: r.date.toISOString(), exitPricePerContract: priceAt, pnl };
        positionOpen = false; break;
      }
      const t_rem = Math.max(1/365, (expiry - r.date)/(365*24*3600*1000));
      const mid = bsPrice(r.close, strike, RISK_FREE, iv, t_rem, optType==='call'?'call':'put');
      const val = mid * contracts * 100;
      const pnl = val - entryCost;
      if(pnl <= -maxLoss){
        exitInfo = { exitDate: r.date.toISOString(), exitPricePerContract: mid, pnl };
        positionOpen = false; break;
      }
      // else continue
    }
    if(positionOpen){
      // if loop ends without expiry reached, close at last available price
      const last = rows[rows.length-1];
      const t2 = Math.max(1/365, (expiry - last.date)/(365*24*3600*1000));
      const mid = bsPrice(last.close, strike, RISK_FREE, iv, t2, optType==='call'?'call':'put');
      const val = mid * contracts * 100;
      const pnl = val - entryCost;
      exitInfo = { exitDate: last.date.toISOString(), exitPricePerContract: mid, pnl };
    }

    trades.push({
      entryDate: entryRow.date.toISOString(), type: optType, strike, entryUnderlying: entryPrice, premium, contracts, entryCost, exit: exitInfo
    });
  }

  // summarize
  let wins=0, losses=0, totPnl=0, grossWins=0, grossLosses=0, maxDraw=0;
  const equity = [5000];
  for(const t of trades){ const p = t.exit.pnl; totPnl+=p; if(p>0){ wins++; grossWins+=p } else { losses++; grossLosses+=p } equity.push(equity[equity.length-1]+p); }
  const maxEq = Math.max(...equity); const minEq = Math.min(...equity); maxDraw = maxEq - minEq;

  const out = { generated: new Date().toISOString(), iv, tradesCount: trades.length, wins, losses, winRate: trades.length? (wins/trades.length):0, totalPnl: totPnl, equityCurve: equity };
  fs.writeFileSync(path.join(__dirname,'..','auto-output','backtest_spy_5m.json'), JSON.stringify(out, null, 2));
  fs.writeFileSync(path.join(__dirname,'..','auto-output','trades_spy_5m.json'), JSON.stringify(trades, null, 2));
  console.log('Backtest complete. Trades:', trades.length, 'Win rate:', out.winRate.toFixed(3));
}

run().catch(e=>{ console.error(e); process.exit(1); });

