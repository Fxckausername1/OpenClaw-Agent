#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const YahooFinance = require('yahoo-finance2').default;
const yf = new YahooFinance();
const argv = Object.fromEntries(process.argv.slice(2).map(s=>s.replace(/^--/,'').split('=')));
const symbol = argv.symbol || 'AAPL';
const days = parseInt(argv.days||'365',10);
const out = argv.out || `trading-stack/${symbol}_yahoo.csv`;
(async()=>{
  try{
    const to = new Date();
    const from = new Date(Date.now() - days*24*3600*1000);
    const opts = { period1: from.toISOString().slice(0,10), period2: to.toISOString().slice(0,10), interval: '1d' };
    const data = await yf.historical(symbol, opts);
    if(!Array.isArray(data) || data.length===0){ console.error('No data returned'); process.exit(2); }
    const rows = ['date,open,high,low,close,adjclose,volume'];
    for(const r of data){
      const dt = new Date(r.date).toISOString();
      rows.push([dt, r.open, r.high, r.low, r.close, r.adjClose || '', r.volume || ''].join(','));
    }
    fs.mkdirSync(path.dirname(out), { recursive:true });
    fs.writeFileSync(out, rows.join('\n'));
    console.log('Saved', out, 'rows=', data.length);
  }catch(e){ console.error('Fetch failed', e.message||e); process.exit(3); }
})();
