"""배송 지연과 리뷰 분석: python -m analysis.run [--out reports/analysis.md]

방법은 결과를 보기 전에 docs/CRITERIA.md '분석 계획'과 DECISIONS D17~D24·D26·D36에 고정했다.
먼저 검사(checks)를 돌려 관문이 하나라도 실패하면 멈추고, 검사가 요구한 민감도를 모두 내는지 확인한다.

- 분석 단위: 주문마다 가장 나중 리뷰, 리뷰 하나가 여러 주문에 붙으면 리뷰당 1행(D23).
  (가) 늦음 여부가 갈리면 제외, (다) 구간이 갈리면 제외, Spearman은 늦은 일수가 갈리면 제외(D24).
- 비교: 평균 리뷰, 1~2점 비율, 95% 신뢰구간(부트스트랩), 늦음 여부 차이, Spearman. p값은 내지 않는다(D20).
- 보고 자릿수: 평균 소수 첫째 자리, 비율 정수 %(D25). Spearman은 소수 둘째 자리.
- 부트스트랩: 리뷰 점수가 1~5 다섯 값뿐이라 집단마다 다항분포로 다시 뽑는 것이 행을 복원 추출하는 것과 같다.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

from checks.core import DSN, SCOPE, check_schema
from checks.run import run_all

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261115
B = 10_000
SENS2_DAYS = 55.5  # 11-13에 고정한 로그 IQR×1.5 상한(D22)
BIN_NAMES = {1: "일찍", 2: "제때", 3: "1~3일", 4: "4~7일", 5: "8일 이상"}


def load_orders(conn, s: str) -> pd.DataFrame:
    """분석 범위의 delivered 주문(+ 민감도 4용 미도착 shipped)과 각 주문의 가장 나중 리뷰."""
    sql = f"""
    WITH o AS (
      SELECT o.order_id, o.order_status, c.customer_state,
             o.order_delivered_customer_date::date - o.order_estimated_delivery_date::date AS late_days,
             extract(epoch FROM o.order_delivered_customer_date::timestamp
                                - o.order_purchase_timestamp::timestamp) / 86400 AS delivery_days,
             date_trunc('hour', o.order_delivered_customer_date::timestamp) AS closed_hour
      FROM {s}.orders o JOIN {s}.customers c USING (customer_id)
      WHERE {SCOPE} AND (
        (o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL)
        OR (o.order_status = 'shipped' AND o.order_delivered_customer_date IS NULL))
    ), long_hours AS (  -- C5: 같은 1시간에 60일 넘은 주문 3건 이상(분석 기간 delivered 기준)
      SELECT date_trunc('hour', o.order_delivered_customer_date::timestamp) AS hr
      FROM {s}.orders o
      WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
        AND o.order_purchase_timestamp::timestamp >= '2017-01-01'
        AND o.order_purchase_timestamp::timestamp < '2018-09-01'
        AND extract(epoch FROM o.order_delivered_customer_date::timestamp
                               - o.order_purchase_timestamp::timestamp) / 86400 > 60
      GROUP BY 1 HAVING count(*) >= 3
    ), r AS (
      SELECT DISTINCT ON (order_id) order_id, review_id, review_score::int AS score
      FROM {s}.order_reviews
      ORDER BY order_id, review_creation_date::timestamp DESC, review_answer_timestamp::timestamp DESC, review_id
    )
    SELECT o.order_id, o.order_status, o.customer_state, o.late_days, o.delivery_days,
           (o.delivery_days > 60 AND o.closed_hour IN (SELECT hr FROM long_hours)) AS c5_flag,
           r.review_id, r.score
    FROM o JOIN r USING (order_id)"""
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)
    df["delivery_days"] = df["delivery_days"].astype(float)
    df["c5_flag"] = df["c5_flag"].map(
        lambda v: v is True
    )  # shipped는 도착 시각이 없어 NULL
    with (ROOT / "reference" / "brazil_regions.csv").open(encoding="utf-8") as f:
        regions = {r["state"]: r["region"] for r in csv.DictReader(f)}
    df["region"] = df["customer_state"].map(regions)
    assert df["region"].notna().all(), "권역이 없는 주"
    return df


def to_reviews(orders: pd.DataFrame) -> pd.DataFrame:
    """리뷰당 1행. 묶인 주문끼리 갈리는지 표시(D23·D24)."""
    o = orders.copy()
    shipped = o["order_status"] == "shipped"
    o["is_late"] = (
        o["late_days"] >= 1
    ) | shipped  # 미도착 shipped는 민감도 4에서만 늦음으로 씀
    o["bin"] = pd.cut(
        o["late_days"], [-np.inf, -1, 0, 3, 7, np.inf], labels=[1, 2, 3, 4, 5]
    ).astype(float)
    g = o.groupby("review_id")
    rv = pd.DataFrame(
        {
            "score": g["score"].max(),
            "score_min": g["score"].min(),
            "all_late": g["is_late"].all(),
            "any_late": g["is_late"].any(),
            "bin_min": g["bin"].min(),
            "bin_max": g["bin"].max(),
            "bin_na": g["bin"].apply(lambda x: x.isna().any()),
            "days_min": g["late_days"].min(),
            "days_max": g["late_days"].max(),
            "days_na": g["late_days"].apply(lambda x: x.isna().any()),
            "region": g["region"].first(),
            "region_n": g["region"].nunique(),
            "has_shipped": g["order_status"].apply(lambda x: (x == "shipped").any()),
        }
    )
    assert (rv["score"] == rv["score_min"]).all(), "같은 리뷰 안에서 점수가 다름"
    assert (rv["region_n"] == 1).all(), "묶인 주문끼리 권역이 다름"
    return rv


def boot(scores: np.ndarray, rng) -> dict:
    """평균·1~2점 비율과 다항분포 부트스트랩 표본."""
    counts = np.bincount(scores, minlength=6)[1:]
    n = int(counts.sum())
    p = counts / n
    sim = rng.multinomial(n, p, size=B)
    vals = np.arange(1, 6)
    return {
        "n": n,
        "mean": float((counts * vals).sum() / n),
        "low": float(p[:2].sum()),
        "mean_b": (sim * vals).sum(axis=1) / n,
        "low_b": sim[:, :2].sum(axis=1) / n,
    }


def ci(x: np.ndarray) -> tuple[float, float]:
    lo, hi = np.percentile(x, [2.5, 97.5])
    return float(lo), float(hi)


def compare_late(rv: pd.DataFrame, rng) -> dict:
    """(가) 늦음 여부: 늦음 vs 안 늦음, 차이(늦음 − 안 늦음)."""
    late = boot(rv.loc[rv["all_late"], "score"].to_numpy(), rng)
    ok = boot(rv.loc[~rv["any_late"], "score"].to_numpy(), rng)
    out = {}
    for name, d in (("late", late), ("ok", ok)):
        out[name] = {
            "n": d["n"],
            "mean": d["mean"],
            "mean_ci": ci(d["mean_b"]),
            "low": d["low"],
            "low_ci": ci(d["low_b"]),
        }
    out["diff_mean"] = late["mean"] - ok["mean"]
    out["diff_mean_ci"] = ci(late["mean_b"] - ok["mean_b"])
    out["diff_low"] = late["low"] - ok["low"]
    out["diff_low_ci"] = ci(late["low_b"] - ok["low_b"])
    return out


def unit_a(rv, include_mixed=False, include_shipped=False):
    """(가) 분석 단위. 기본은 갈리는 리뷰 제외, 미도착 shipped가 섞인 리뷰 제외."""
    keep = rv["all_late"] == rv["any_late"]
    if include_mixed:  # 민감도 5: 갈리는 리뷰를 늦음으로
        rv = rv.assign(all_late=rv["all_late"] | (rv["any_late"] & ~keep))
        keep = pd.Series(True, index=rv.index)
    if not include_shipped:
        keep &= ~rv["has_shipped"]
    return rv[keep]


def bins(rv, rng) -> list[dict]:
    """(다) 구간별. 구간이 갈리거나 늦은 일수가 없는(shipped) 리뷰는 제외."""
    u = rv[(rv["bin_min"] == rv["bin_max"]) & ~rv["bin_na"]]
    out = []
    for b in range(1, 6):
        d = boot(u.loc[u["bin_min"] == b, "score"].to_numpy(), rng)
        out.append(
            {
                "bin": BIN_NAMES[b],
                "n": d["n"],
                "mean": d["mean"],
                "mean_ci": ci(d["mean_b"]),
                "low": d["low"],
                "low_ci": ci(d["low_b"]),
            }
        )
    return out


def spearman(rv) -> tuple[float, int]:
    u = rv[(rv["days_min"] == rv["days_max"]) & ~rv["days_na"]]
    # Spearman = 평균 순위(동점은 평균)끼리의 피어슨 상관. scipy 없이 정의대로 계산한다.
    x = u["days_min"].astype(float).rank(method="average")
    y = u["score"].astype(float).rank(method="average")
    return float(np.corrcoef(x, y)[0, 1]), len(u)


def analyse(orders: pd.DataFrame) -> dict:
    rng = np.random.default_rng(SEED)
    delivered = orders[orders["order_status"] == "delivered"]
    sets = {
        "본 결과": delivered,
        "민감도 1(C5 일괄 도착 처리 제외)": delivered[~delivered["c5_flag"]],
        "민감도 2(배송 55.5일 초과 제외)": delivered[
            ~(delivered["delivery_days"] > SENS2_DAYS)
        ],
    }
    res = {
        "excluded_orders": {
            "민감도 1": int(delivered["c5_flag"].sum()),
            "민감도 2": int((delivered["delivery_days"] > SENS2_DAYS).sum()),
        }
    }
    for name, o in sets.items():
        rv = to_reviews(o)
        sp, sp_n = spearman(rv)
        res[name] = {
            "a": compare_late(unit_a(rv), rng),
            "c": bins(rv, rng),
            "spearman": sp,
            "spearman_n": sp_n,
        }
    rv_all = to_reviews(orders)  # shipped 포함
    rv_del = to_reviews(delivered)
    res["민감도 3(권역별 층화, (가))"] = {
        reg: compare_late(unit_a(rv_del[rv_del["region"] == reg]), rng)
        for reg in ["Sudeste", "Sul", "Nordeste", "Centro-Oeste", "Norte"]
    }
    res["민감도 4(미도착 shipped를 늦음으로, (가))"] = compare_late(
        unit_a(rv_all, include_shipped=True), rng
    )
    res["민감도 5(갈리는 리뷰를 늦음으로, (가))"] = compare_late(
        unit_a(rv_del, include_mixed=True), rng
    )
    return res


def m(x):  # 평균: 소수 첫째 자리(D25)
    return f"{x:.1f}"


def pct(x):  # 비율: 정수 %(D25)
    return f"{x * 100:.0f}%"


def pp(x):
    return f"{x * 100:+.0f}%p"


def ci_m(c):
    return f"[{c[0]:.1f}, {c[1]:.1f}]"


def ci_p(c):
    return f"[{c[0] * 100:.0f}, {c[1] * 100:.0f}]"


def ci_pp(c):
    return f"[{c[0] * 100:+.0f}, {c[1] * 100:+.0f}]"


def ci_d(c):
    return f"[{c[0]:+.1f}, {c[1]:+.1f}]"


def a_rows(label, a):
    return (
        f"| {label} | {a['late']['n']:,} | {m(a['late']['mean'])} {ci_m(a['late']['mean_ci'])} | "
        f"{pct(a['late']['low'])} {ci_p(a['late']['low_ci'])} | {a['ok']['n']:,} | "
        f"{m(a['ok']['mean'])} {ci_m(a['ok']['mean_ci'])} | {pct(a['ok']['low'])} {ci_p(a['ok']['low_ci'])} | "
        f"{a['diff_mean']:+.1f} {ci_d(a['diff_mean_ci'])} | {pp(a['diff_low'])} {ci_pp(a['diff_low_ci'])} |"
    )


def render(res: dict, checks_summary: str) -> str:
    L = [
        "# 분석 결과(생성 파일, 직접 고치지 않음)",
        "",
        f"`python -m analysis.run`. 부트스트랩 {B:,}회(시드 {SEED}), 95% 구간 = 2.5·97.5 백분위. "
        "평균은 소수 첫째 자리, 비율은 정수 %(D25). 결론은 관계로만 쓴다(인과 아님).",
        "",
        checks_summary,
        "",
        "## (가) 늦음 여부",
        "",
        "| 묶음 | 늦음 리뷰 | 늦음 평균 [95%] | 늦음 1~2점 [95%] | 안 늦음 리뷰 | 안 늦음 평균 [95%] | 안 늦음 1~2점 [95%] | 평균 차이 [95%] | 1~2점 차이 [95%] |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name in (
        "본 결과",
        "민감도 1(C5 일괄 도착 처리 제외)",
        "민감도 2(배송 55.5일 초과 제외)",
    ):
        L.append(a_rows(name, res[name]["a"]))
    for reg, a in res["민감도 3(권역별 층화, (가))"].items():
        L.append(a_rows(f"민감도 3 {reg}", a))
    L.append(
        a_rows(
            "민감도 4(미도착 shipped = 늦음)",
            res["민감도 4(미도착 shipped를 늦음으로, (가))"],
        )
    )
    L.append(
        a_rows(
            "민감도 5(갈리는 리뷰 = 늦음)",
            res["민감도 5(갈리는 리뷰를 늦음으로, (가))"],
        )
    )
    L += [
        "",
        f"제외한 주문: 민감도 1 = {res['excluded_orders']['민감도 1']}건, "
        f"민감도 2 = {res['excluded_orders']['민감도 2']}건.",
        "",
        "## (다) 지연 구간",
        "",
    ]
    for name in (
        "본 결과",
        "민감도 1(C5 일괄 도착 처리 제외)",
        "민감도 2(배송 55.5일 초과 제외)",
    ):
        L += [
            f"### {name}",
            "",
            "| 구간 | 리뷰 | 평균 [95%] | 1~2점 [95%] |",
            "|---|---|---|---|",
        ]
        for b in res[name]["c"]:
            L.append(
                f"| {b['bin']} | {b['n']:,} | {m(b['mean'])} {ci_m(b['mean_ci'])} | {pct(b['low'])} {ci_p(b['low_ci'])} |"
            )
        L.append("")
    L += [
        "## Spearman(늦은 일수 ↔ 리뷰 점수)",
        "",
        "| 묶음 | 리뷰 | ρ |",
        "|---|---|---|",
    ]
    for name in (
        "본 결과",
        "민감도 1(C5 일괄 도착 처리 제외)",
        "민감도 2(배송 55.5일 초과 제외)",
    ):
        L.append(
            f"| {name} | {res[name]['spearman_n']:,} | {res[name]['spearman']:.2f} |"
        )
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schema", default="raw")
    ap.add_argument("--out", type=Path, default=Path("reports/analysis.md"))
    args = ap.parse_args(argv)
    s = check_schema(args.schema)
    with psycopg.connect(DSN) as conn:
        results = run_all(conn, s)
        conn.rollback()
        failed = [r.id for r in results if r.role == "gate" and r.status == "fail"]
        if failed:
            print(f"관문 실패로 분석하지 않음: {', '.join(failed)}")
            return 1
        required = sorted({x for r in results for x in r.requires})
        produced = {"민감도 4", "민감도 5"}
        missing = [x for x in required if x not in produced]
        if missing:
            print(f"검사가 요구한 민감도를 내지 않음: {missing}")
            return 1
        orders = load_orders(conn, s)
    res = analyse(orders)
    summary = f"검사: 관문 실패 없음, 검사가 요구한 민감도 = {', '.join(required) or '없음'}(모두 아래에 있음)."
    text = render(res, summary)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    args.out.with_suffix(".json").write_text(
        json.dumps(res, ensure_ascii=False, indent=1, default=float), encoding="utf-8"
    )
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
