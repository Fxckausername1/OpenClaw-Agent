#!/bin/bash
cd /home/heff/.openclaw/workspace || exit 1
./venv/bin/python -u databento_options.py --budget 100 --arm --stats --universe F BAC INTC PFE CSCO AMD PLTR XLF GDX HOOD NVDA >> data/options/pull.log 2>&1
