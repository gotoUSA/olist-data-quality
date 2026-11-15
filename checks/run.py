"""검사 실행: python -m checks.run [--schema raw] [--json reports/checks.json]

관문(gate) 검사가 하나라도 실패하면 종료 코드 1. C0(형 변환)이 실패하면 나머지는 돌지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

import psycopg

from .catalog import ALL
from .core import DSN, check_schema


def run_all(conn, schema: str):
    s = check_schema(schema)
    results = []
    with conn.cursor() as cur:
        for fn in ALL:
            r = fn(cur, s)
            results.append(r)
            if r.id == "C0" and r.status == "fail":
                break
    return results


def fmt_details(d: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="raw")
    ap.add_argument("--json", type=Path)
    args = ap.parse_args(argv)

    t0 = time.time()
    with psycopg.connect(DSN) as conn:
        results = run_all(conn, args.schema)
    elapsed = time.time() - t0

    print("| 검사 | 역할 | 결과 | 건수 | 세부 | 비고 |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r.id} {r.title} | {r.role} | {r.status} | {r.count:,} | {fmt_details(r.details)} | {r.note} |"
        )
    failed = [r.id for r in results if r.role == "gate" and r.status == "fail"]
    print(
        f"\n관문 실패: {', '.join(failed) if failed else '없음'} ({elapsed:.1f}초, schema={args.schema})"
    )

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
