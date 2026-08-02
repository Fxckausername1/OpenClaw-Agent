#!/usr/bin/env node
// outreach-automation draft generator
// Usage: node generate_draft.mjs --name "Artist" --handle "insta"

const args = process.argv.slice(2);
console.log('outreach-automation: generating draft with', args.join(' '));
console.log('Output: {subject,body,followUpDays}');

