#!/usr/bin/env node
// Append new rows from data/paper_trades.csv into the Google Sheet (Trades tab).
import {readFileSync, writeFileSync, existsSync} from 'fs'
import {createSign} from 'crypto'
import fetch from 'node-fetch'
import path from 'path'

const SA_PATH = process.env.GOOGLE_SA_KEY_FILE || 'credentials/gcp_sa.json'
const SHEET_ID_PATH = 'data/sheet_id.txt'
const CSV_PATH = 'data/paper_trades.csv'
const CURSOR_PATH = 'data/sheet_ingest_cursor.json'

function loadKey(){
  if (existsSync(SA_PATH)) return JSON.parse(readFileSync(SA_PATH,'utf8'))
  if (process.env.GOOGLE_SA_KEY_JSON) return JSON.parse(process.env.GOOGLE_SA_KEY_JSON)
  throw new Error('service account key not found')
}

function base64url(input){
  return Buffer.from(input).toString('base64').replace(/=+$/,'').replace(/\+/g,'-').replace(/\//g,'_')
}

async function getAccessToken(sa){
  const iat=Math.floor(Date.now()/1000), exp=iat+3600
  const header={alg:'RS256', typ:'JWT'}
  const payload={iss:sa.client_email, scope:'https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/drive.file', aud:sa.token_uri, iat, exp}
  const toSign=`${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(payload))}`
  const sign=createSign('RSA-SHA256')
  sign.update(toSign)
  const signature=sign.sign(sa.private_key,'base64').replace(/=+$/,'').replace(/\+/g,'-').replace(/\//g,'_')
  const jwt=`${toSign}.${signature}`
  const resp=await fetch(sa.token_uri, {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${jwt}`})
  if(!resp.ok) throw new Error('token exchange failed')
  const j=await resp.json(); return j.access_token
}

function parseCsvAll(pathStr){
  const txt=readFileSync(pathStr,'utf8')
  const lines=txt.split(/\r?\n/).filter(Boolean)
  if(lines.length<2) return {header:[], rows:[]}
  const header=lines[0].split(',')
  const rows=lines.slice(1).map(l=>{ // simple CSV split — values contain no commas in our use
    return l.split(',')
  })
  return {header, rows}
}

async function appendValues(sheetId, values){
  const sa=loadKey()
  const token=await getAccessToken(sa)
  const url = `https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Trades!A1:append?valueInputOption=RAW`;
  const body = {values}
  const resp = await fetch(url, {method:'POST', headers:{'Authorization':`Bearer ${token}`,'Content-Type':'application/json'}, body:JSON.stringify(body)})
  if(!resp.ok) throw new Error('append failed: '+await resp.text())
  return await resp.json()
}

async function main(){
  if(!existsSync(SHEET_ID_PATH)) throw new Error('data/sheet_id.txt not found — run create_sheet.mjs first')
  const sheetId = readFileSync(SHEET_ID_PATH,'utf8').trim()
  if(!existsSync(CSV_PATH)) throw new Error('no paper_trades.csv')
  const {header, rows} = parseCsvAll(CSV_PATH)
  // cursor: last appended trade_id
  let cursor=null
  if (existsSync(CURSOR_PATH)) try{ cursor=JSON.parse(readFileSync(CURSOR_PATH,'utf8')).last_trade_id }catch(e){}
  // find index to start
  let startIdx=0
  if(cursor){
    for(let i=0;i<rows.length;i++){
      if(rows[i][0]===cursor){ startIdx=i+1 }
    }
  }
  const toAppend = rows.slice(startIdx)
  if(toAppend.length===0){ console.log('no new rows'); return }
  // append
  await appendValues(sheetId, toAppend)
  const last = toAppend[toAppend.length-1][0]
  writeFileSync(CURSOR_PATH, JSON.stringify({last_trade_id:last}))
  console.log('appended', toAppend.length, 'rows')
}

main().catch(e=>{console.error(e.message); process.exit(1)})

