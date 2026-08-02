#!/usr/bin/env node
// Create a Google Sheets spreadsheet using a service account JSON key.
// Writes the created sheetId to data/sheet_id.txt
import {readFileSync, writeFileSync, existsSync} from 'fs'
import {createSign} from 'crypto'
import path from 'path'

const KEY_PATHS = [process.env.GOOGLE_SA_KEY_FILE || 'credentials/gcp_sa.json']

function loadKey() {
  for (const p of KEY_PATHS) {
    try {
      if (existsSync(p)) return JSON.parse(readFileSync(p, 'utf8'))
    } catch (e) {}
  }
  if (process.env.GOOGLE_SA_KEY_JSON) return JSON.parse(process.env.GOOGLE_SA_KEY_JSON)
  throw new Error('No service account key found. Set GOOGLE_SA_KEY_FILE or GOOGLE_SA_KEY_JSON')
}

function base64url(input) {
  return Buffer.from(input).toString('base64').replace(/=+$/,'').replace(/\+/g,'-').replace(/\//g,'_')
}

async function getAccessToken(sa) {
  const iat = Math.floor(Date.now()/1000)
  const exp = iat + 3600
  const header = {alg: 'RS256', typ: 'JWT'}
  const scope = ['https://www.googleapis.com/auth/spreadsheets', 'https://www.googleapis.com/auth/drive.file'].join(' ')
  const payload = {
    iss: sa.client_email,
    scope,
    aud: sa.token_uri,
    exp,
    iat
  }
  const toSign = `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(payload))}`
  const sign = createSign('RSA-SHA256')
  sign.update(toSign)
  const signature = sign.sign(sa.private_key, 'base64').replace(/=+$/,'').replace(/\+/g,'-').replace(/\//g,'_')
  const jwt = `${toSign}.${signature}`
  const resp = await fetch(sa.token_uri, {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${jwt}`})
  if (!resp.ok) throw new Error('token exchange failed: '+await resp.text())
  const j = await resp.json()
  return j.access_token
}

async function createSheet(title='Paper Trades'){
  const sa = loadKey()
  const token = await getAccessToken(sa)
  const body = {
    properties: {title},
    sheets: [{properties:{title:'Trades'}}]
  }
  const resp = await fetch('https://sheets.googleapis.com/v4/spreadsheets', {method:'POST', headers:{'Authorization':`Bearer ${token}`, 'Content-Type':'application/json'}, body:JSON.stringify(body)})
  if (!resp.ok) throw new Error('create sheet failed: '+await resp.text())
  const j = await resp.json()
  return {id:j.spreadsheetId, url:j.spreadsheetUrl}
}

async function writeHeader(sheetId, values) {
  const sa = loadKey()
  const token = await getAccessToken(sa)
  const url = `https://sheets.googleapis.com/v4/spreadsheets/${sheetId}/values/Trades!A1:append?valueInputOption=RAW`;
  const body = {values:[values]}
  const resp = await fetch(url, {method:'POST', headers:{'Authorization':`Bearer ${token}`,'Content-Type':'application/json'}, body:JSON.stringify(body)})
  if (!resp.ok) throw new Error('write header failed: '+await resp.text())
}

async function main(){
  const title = process.argv[2] || `Paper Trades ${new Date().toISOString().slice(0,10)}`
  const sheet = await createSheet(title)
  const headers = ['trade_id','ticker','side','entry','stop','t1','t2','planned_rr','entry_time','exit_price','exit_reason','outcome_r','close_time','dollar_pnl']
  await writeHeader(sheet.id, headers)
  const outPath = path.join('data','sheet_id.txt')
  writeFileSync(outPath, sheet.id)
  console.log('created', sheet.id, sheet.url)
}

main().catch(e=>{console.error(e.message); process.exit(1)})
