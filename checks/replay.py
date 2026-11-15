"""replay 백테스트: python -m checks.replay [--out reports/replay.md]

운영 흉내(원칙 0, D34). 준비 기간 2017-01~06은 기준선에만 쓰고, 2017-07~2018-08을 날짜순으로 돌며
그 시점 이전 데이터로만 기준선을 잡는다.

- 월별(원칙 2): 그 달 건수가 기준선 비율로 기대되는 건수보다 많을 확률 P(X ≥ 관측) < 1%이면 경고.
  기준선 = 준비 기간 시작부터 지난달까지 누적. 기준선 사건이 0건이면 '기준선 부족'(D34).
- 일별: C15(승인 지연 중앙값 3시간 경고·12시간 실패, D14)와 C16(지난 14일 중앙값 대비 2배·0.5배 경고, D33).

한계: 데이터는 최종 상태 하나뿐이라, 그날 시점에 실제로 보였을 상태(나중에 채워진 도착일 등)가 아니라
최종 상태를 날짜순으로 흘린다.
"""

from __future__ import annotations

import argparse
import math
import statistics
from datetime import date, timedelta
from pathlib import Path

import psycopg

from .core import DSN, PERIOD, SCOPE, check_schema

PREP_START = date(2017, 1, 1)
REPLAY_START = date(2017, 7, 1)
REPLAY_END = date(2018, 8, 31)
P_WARN = 0.01

# 월별 건수형 검사: (id, 이름, 분모 SQL, 사건 SQL). 둘 다 구매월(m)별로 센다.
MONTHLY = {
    "C1": (
        "delivered인데 도착일 없음",
        f"FROM {{s}}.orders o WHERE o.order_status = 'delivered' AND {SCOPE}",
        "o.order_delivered_customer_date IS NULL",
    ),
    "C2s": (
        "예상일 지난 미도착 shipped(운영 설계, D26)",
        f"FROM {{s}}.orders o WHERE o.order_status IN ('delivered', 'shipped') AND {SCOPE}",
        "o.order_status = 'shipped' AND o.order_delivered_customer_date IS NULL",
    ),
    "C4": (
        "도착이 택배 인계보다 1시간 이상 앞섬",
        f"FROM {{s}}.orders o WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL AND {SCOPE}",
        "o.order_delivered_customer_date::timestamp <= o.order_delivered_carrier_date::timestamp - interval '1 hour'",
    ),
    "C6": (
        "카테고리 없는 상품의 주문 품목",
        f"FROM {{s}}.orders o JOIN {{s}}.order_items i USING (order_id) "
        f"LEFT JOIN {{s}}.products p USING (product_id) WHERE {PERIOD}",
        "p.product_category_name IS NULL",
    ),
}


def poisson_sf(x: int, lam: float) -> float:
    """P(X ≥ x), X ~ Poisson(lam)."""
    if x <= 0:
        return 1.0
    term = math.exp(-lam)
    cdf = term
    for k in range(1, x):
        term *= lam / k
        cdf += term
    return max(0.0, 1.0 - cdf)


def month_iter(start: date, end: date):
    d = date(start.year, start.month, 1)
    while d <= end:
        yield d
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def monthly(cur, s: str):
    out = []
    for cid, (name, base_sql, event_sql) in MONTHLY.items():
        cur.execute(
            f"""SELECT date_trunc('month', o.order_purchase_timestamp::timestamp)::date AS m,
                       count(*), count(*) FILTER (WHERE {event_sql})
                {base_sql.format(s=s)} GROUP BY 1 ORDER BY 1"""
        )
        by_month = {m: (n, k) for m, n, k in cur.fetchall()}
        for m in month_iter(REPLAY_START, REPLAY_END):
            hist = [v for mm, v in by_month.items() if PREP_START <= mm < m]
            base_n = sum(n for n, _ in hist)
            base_k = sum(k for _, k in hist)
            n, k = by_month.get(m, (0, 0))
            if base_k == 0:
                status, lam, p = "기준선 부족", None, None
            else:
                lam = base_k / base_n * n
                p = poisson_sf(k, lam)
                status = "경고" if p < P_WARN else "통과"
            out.append(
                {
                    "id": cid,
                    "name": name,
                    "month": m,
                    "n": n,
                    "k": k,
                    "base": f"{base_k}/{base_n}",
                    "expected": lam,
                    "p": p,
                    "status": status,
                }
            )
    return out


def daily(cur, s: str):
    # 일별 구매 건수(C16)와 그날 구매의 승인 지연 중앙값(C15)
    cur.execute(
        f"""SELECT g::date,
                   (SELECT count(*) FROM {s}.orders o WHERE o.order_purchase_timestamp::date = g::date)
            FROM generate_series(%s::date, %s::date, '1 day') g""",
        (PREP_START, REPLAY_END),
    )
    counts = dict(cur.fetchall())
    cur.execute(
        f"""SELECT o.order_purchase_timestamp::date, count(*),
                   percentile_cont(0.5) WITHIN GROUP (ORDER BY coalesce(
                     extract(epoch FROM o.order_approved_at::timestamp - o.order_purchase_timestamp::timestamp) / 3600,
                     1e9)::float)
            FROM {s}.orders o WHERE {PERIOD} GROUP BY 1"""
    )
    approval = {d: (n, med) for d, n, med in cur.fetchall()}

    out = []
    d = REPLAY_START
    while d <= REPLAY_END:
        prev = [counts[d - timedelta(days=i)] for i in range(1, 15)]
        med14 = statistics.median(prev)
        ratio = counts[d] / med14 if med14 else None
        if ratio is not None and (ratio >= 2 or ratio <= 0.5):
            out.append(
                {
                    "id": "C16",
                    "day": d,
                    "value": f"{counts[d]}건 / 지난 14일 중앙값 {med14:g} = {ratio:.2f}배",
                    "status": "경고",
                }
            )
        n, med = approval.get(d, (0, None))
        if n >= 50 and med is not None and med >= 3:
            out.append(
                {
                    "id": "C15",
                    "day": d,
                    "value": f"구매 {n}건, 승인 지연 중앙값 {med:.1f}시간",
                    "status": "실패(운영)" if med >= 12 else "경고",
                }
            )
        d += timedelta(days=1)
    return out


def render(mon, day) -> str:
    lines = [
        "# replay 결과(생성 파일, 직접 고치지 않음)",
        "",
        f"준비 기간 {PREP_START}~{REPLAY_START - timedelta(days=1)}, 검사 구간 {REPLAY_START}~{REPLAY_END}. "
        "월별 = 구매월 기준, 기준선 = 준비 기간 시작~지난달 누적 비율, 경고 = 포아송 P(X ≥ 관측) < 1%.",
        "",
        "## 월별 경고·기준선 부족",
        "",
        "| 검사 | 월 | 건수/분모 | 기준선(사건/분모) | 기대 건수 | P | 결과 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in mon:
        if r["status"] == "통과":
            continue
        exp = "" if r["expected"] is None else f"{r['expected']:.2f}"
        p = "" if r["p"] is None else f"{r['p']:.4f}"
        lines.append(
            f"| {r['id']} | {r['month']:%Y-%m} | {r['k']}/{r['n']} | {r['base']} | {exp} | {p} | {r['status']} |"
        )
    lines += [
        "",
        "## 월별 요약",
        "",
        "| 검사 | 통과 | 경고 | 기준선 부족 |",
        "|---|---|---|---|",
    ]
    for cid in MONTHLY:
        rs = [r for r in mon if r["id"] == cid]
        lines.append(
            f"| {cid} {MONTHLY[cid][0]} | "
            + " | ".join(
                str(sum(r["status"] == st for r in rs))
                for st in ("통과", "경고", "기준선 부족")
            )
            + " |"
        )
    lines += [
        "",
        "## 일별 경고(C15·C16)",
        "",
        "| 검사 | 날짜 | 요일 | 값 | 결과 |",
        "|---|---|---|---|---|",
    ]
    for r in day:
        lines.append(
            f"| {r['id']} | {r['day']} | {'월화수목금토일'[r['day'].weekday()]} | {r['value']} | {r['status']} |"
        )
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="raw")
    ap.add_argument("--out", type=Path, default=Path("reports/replay.md"))
    args = ap.parse_args(argv)
    s = check_schema(args.schema)
    with psycopg.connect(DSN) as conn, conn.cursor() as cur:
        mon, day = monthly(cur, s), daily(cur, s)
    text = render(mon, day)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
