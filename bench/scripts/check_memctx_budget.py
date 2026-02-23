#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', required=True)
    ap.add_argument('--budget', type=int, default=120)
    ap.add_argument('--max-hybrid-input', type=int, default=20000)
    args = ap.parse_args()

    bad_budget = 0
    bad_input = 0
    total = 0
    with open(args.csv, newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        for row in r:
            total += 1
            mode = row.get('mode', '')
            memctx = row.get('memctx_tokens_est')
            if memctx not in (None, '', '0'):
                try:
                    if float(memctx) > args.budget:
                        bad_budget += 1
                except ValueError:
                    pass
            if mode == 'hybrid_memctx':
                try:
                    inp = float(row.get('input_tokens', 0) or 0)
                    if inp > args.max_hybrid_input:
                        bad_input += 1
                except ValueError:
                    pass

    print({'rows': total, 'bad_budget': bad_budget, 'bad_hybrid_input': bad_input})
    if bad_budget or bad_input:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
