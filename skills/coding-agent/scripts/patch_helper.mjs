#!/usr/bin/env node
// Lightweight helper to create a unified diff between original and modified content.
// Usage (example):
//   node patch_helper.mjs --old-file path/to/orig --new-file path/to/new
// Prints a unified diff to stdout.

import { spawnSync } from 'child_process';
import fs from 'fs';

function usage() {
  console.error('Usage: patch_helper.mjs --old-file <path> --new-file <path>');
  process.exit(1);
}

const args = process.argv.slice(2);
const oldIdx = args.indexOf('--old-file');
const newIdx = args.indexOf('--new-file');
if (oldIdx === -1 || newIdx === -1) usage();
const oldPath = args[oldIdx + 1];
const newPath = args[newIdx + 1];
if (!oldPath || !newPath) usage();

if (!fs.existsSync(oldPath)) { console.error('Old file not found:', oldPath); process.exit(2); }
if (!fs.existsSync(newPath)) { console.error('New file not found:', newPath); process.exit(2); }

const gitDiff = spawnSync('git', ['--no-pager', 'diff', '--no-index', '--', oldPath, newPath], { encoding: 'utf8' });
if (gitDiff.status === 0 || gitDiff.stdout) {
  console.log(gitDiff.stdout || '');
  process.exit(0);
}
// if git diff failed, fallback to simple output
const oldContent = fs.readFileSync(oldPath, 'utf8');
const newContent = fs.readFileSync(newPath, 'utf8');
console.log('--- ' + oldPath);
console.log('+++ ' + newPath);
console.log('@@ (showing full replacement) @@');
console.log(newContent);

