#!/usr/bin/env node
// crm-sync stub
// Usage: node sync_stub.mjs --sheet <sheetId> --dry-run
const args = process.argv.slice(2);
console.log('crm-sync: stub run with', args.join(' '));
console.log('Behavior: maps sheet columns to CRM fields and performs dry-run unless --live is provided (requires API keys)');

