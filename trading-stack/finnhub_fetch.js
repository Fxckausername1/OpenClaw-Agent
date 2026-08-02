#!/usr/bin/env node
// usage: node trading-stack/finnhub_fetch.js --symbol=AAPL --apikey=YOUR_KEY --resolution=D --days=365 --out=out.csv
const axios = require('axios');
const fs = require('fs');
const argv = Object.fromEntries(process.argv.slice(2).map(s=>s.replace(/^--/,'').split('=')));
const symbol = argv.symbol || 'AAPL';
const apiKey = argv.apikey || process.env.FINNHUB_API_KEY;
const resolution = argv.resolution || 'D';
const days = parseInt(argv.days||'365',10);
const out = argv.out || `trading-stack/${symbol}_finnhub.csv`;
if(!apiKey){ console.error('Missing api key (use --apikey or set FINNHUB_API_KEY)'); process.exit(1); }
(async()=>{
  try{
    const to = Math.floor(Date.now()/1000);
    const from = to - days*24*3600;
    const url = 'https://finnhub.io/api/v1/stock/candle';
    const r = await axios.get(url, { params:{ symbol, resolution, from, to, token: apiKey }, timeout:30000 });
    const j = r.data;
    if(j.s !== 'ok'){ console.error('Finnhub error', j); process.exit(2); }
    const rows = ['date,open,high,low,close,volume'];
    for(let i=0;i<j.t.length;i++){
      const dt = new Date(j.t[i]*1000).toISOString();
      rows.push([dt, j.o[i], j.h[i], j.l[i], j.c[i], j.v[i]].join(','));
    }
    fs.mkdirSync(require('path').dirname(out), { recursive:true });
    fs.writeFileSync(out, rows.join('\n'));
    console.log('Saved', out, 'rows=', j.t.length);
  }catch(e){
    console.error('Fetch failed', e.message || e);
    process.exit(3);
  }
})();

