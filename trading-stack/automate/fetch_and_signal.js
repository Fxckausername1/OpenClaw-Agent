#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const YahooFinance = require('yahoo-finance2').default;
const SMA = require('technicalindicators').SMA;
const yf = new YahooFinance({ suppressNotices: ['ripHistorical'] });

const symbolsFile = path.resolve(__dirname, '..', 'symbols.json');
const outDir = path.resolve(__dirname, '..', 'auto-output');
if(!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

async function fetchHistory(sym, days=365){
  const to = new Date();
  const from = new Date(Date.now() - days*24*3600*1000);
  const opts = { period1: from.toISOString().slice(0,10), period2: to.toISOString().slice(0,10), interval: '1d' };
  const data = await yf.chart(sym, opts).catch(e=>{ throw e; });
  // chart() returns structured data; fallback to historical mapping used earlier
  const rows = (data && data.result && data.result[0] && data.result[0].indicators && data.result[0].indicators.quote && data.result[0].indicators.quote[0]) ?
    data.result[0].timestamp.map((t,i)=>({ date: new Date(data.result[0].timestamp[i]*1000).toISOString(), open: data.result[0].indicators.quote[0].open[i], high: data.result[0].indicators.quote[0].high[i], low: data.result[0].indicators.quote[0].low[i], close: data.result[0].indicators.quote[0].close[i], volume: data.result[0].indicators.quote[0].volume[i] })) : [];
  return rows;
}

function computeSmaSignal(closes, short=10, long=50){
  if(closes.length < long) return { signal: 'insufficient_data' };
  const shortSma = SMA.calculate({ period: short, values: closes });
  const longSma = SMA.calculate({ period: long, values: closes });
  const lastShort = shortSma[shortSma.length-1];
  const lastLong = longSma[longSma.length-1];
  const signal = lastShort > lastLong ? 'BUY' : (lastShort < lastLong ? 'SELL' : 'HOLD');
  return { signal, lastShort, lastLong };
}

async function run(){
  const cfg = JSON.parse(fs.readFileSync(symbolsFile,'utf8'));
  const results = [];
  for(const sym of cfg.symbols){
    try{
      const rows = await fetchHistory(sym, 365);
      if(!rows || rows.length===0){ results.push({ symbol: sym, error: 'no_data' }); continue; }
      const closes = rows.map(r=>r.close).filter(v=>v!=null);
      const sig = computeSmaSignal(closes, 10, 50);
      const out = { symbol: sym, signal: sig.signal, lastShort: sig.lastShort, lastLong: sig.lastLong, date: new Date().toISOString() };
      results.push(out);
      // save per-symbol csv
      const csvPath = path.join(outDir, `${sym.replace('/','_')}.csv`);
      const csvRows = ['date,open,high,low,close,volume'];
      for(const r of rows) csvRows.push([r.date, r.open, r.high, r.low, r.close, r.volume].join(','));
      fs.writeFileSync(csvPath, csvRows.join('\n'));
    }catch(e){
      results.push({ symbol: sym, error: String(e) });
    }
  }
  const outPath = path.join(outDir, 'signals.json');
  fs.writeFileSync(outPath, JSON.stringify({ generated: new Date().toISOString(), results }, null, 2));
  console.log('Wrote', outPath);
}

if(require.main === module) run().catch(e=>{ console.error('Failed', e); process.exit(1); });

