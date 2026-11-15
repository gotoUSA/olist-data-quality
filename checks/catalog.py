"""검사 C0~C18(C16은 replay에서만). 각 함수는 (cursor, schema) → Result.

role="gate"는 본 분석에 쓰는 열의 관문이라 실패하면 분석을 멈춘다(원칙 0).
role="report"는 이 고정 데이터에서는 보고만 하고, '운영이라면' 규칙에 걸리는 건수는
details["ops_*"]에 따로 적는다.
"""

from __future__ import annotations

import csv
from pathlib import Path

from .core import (
    PERIOD,
    SCOPE,
    Result,
    analysis_base_cte,
    gate_threshold,
    late_group_size,
    one,
    rows,
)

REFERENCE = Path(__file__).resolve().parent.parent / "reference"

# C0: 검사·분석이 형 변환하는 열(D1: raw는 전 열 TEXT라 변환 실패 값을 여기서 센다)
TYPED_COLUMNS = {
    "orders": {
        "order_purchase_timestamp": "timestamp",
        "order_approved_at": "timestamp",
        "order_delivered_carrier_date": "timestamp",
        "order_delivered_customer_date": "timestamp",
        "order_estimated_delivery_date": "timestamp",
    },
    "order_items": {"price": "numeric", "freight_value": "numeric"},
    "order_payments": {"payment_value": "numeric", "payment_installments": "integer"},
    "order_reviews": {
        "review_creation_date": "timestamp",
        "review_answer_timestamp": "timestamp",
    },
    "products": {
        "product_weight_g": "numeric",
        "product_length_cm": "numeric",
        "product_height_cm": "numeric",
        "product_width_cm": "numeric",
    },
    "geolocation": {
        "geolocation_lat": "double precision",
        "geolocation_lng": "double precision",
    },
}


def c0_types(cur, s):
    bad = {}
    for table, cols in TYPED_COLUMNS.items():
        exprs = ", ".join(
            f"count(*) FILTER (WHERE {c} IS NOT NULL AND NOT pg_input_is_valid({c}, '{t}'))"
            for c, t in cols.items()
        )
        counts = one(cur, f"SELECT {exprs} FROM {s}.{table}")
        for c, n in zip(cols, counts):
            if n:
                bad[f"{table}.{c}"] = n
    total = sum(bad.values())
    return Result(
        "C0",
        "형 변환 실패 값",
        "gate",
        "fail" if total else "pass",
        total,
        bad,
        "다른 검사가 이 열들을 형 변환하므로 먼저 돈다",
    )


def c1_delivered_no_date(cur, s):
    k = one(
        cur,
        f"""SELECT count(*) FROM {s}.orders o
        WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NULL AND {SCOPE}""",
    )[0]
    total = one(
        cur,
        f"""SELECT count(*) FROM {s}.orders
        WHERE order_status = 'delivered' AND order_delivered_customer_date IS NULL""",
    )[0]
    n = late_group_size(cur, s)
    limit = gate_threshold(n)
    return Result(
        "C1",
        "delivered인데 도착일 없음",
        "gate",
        "fail" if k >= limit else "pass",
        k,
        {"전체": total, "분석 범위": k, "늦은 리뷰 n": n, "실패 기준(건 이상)": limit},
        "D25 최악 영향 기준",
    )


def c2_no_date_by_status(cur, s):
    by = rows(
        cur,
        f"""SELECT o.order_status, count(*),
            count(*) FILTER (WHERE o.order_delivered_carrier_date IS NOT NULL)
        FROM {s}.orders o WHERE o.order_delivered_customer_date IS NULL AND {SCOPE}
        GROUP BY 1 ORDER BY 2 DESC""",
    )
    details = {st: n for st, n, _ in by}
    details["canceled 중 택배 인계 있음"] = next(
        (c for st, _, c in by if st == "canceled"), 0
    )
    shipped = details.get("shipped", 0)
    return Result(
        "C2",
        "도착일 없음(상태별, 분석 범위)",
        "report",
        "report",
        sum(n for _, n, _ in by),
        details,
        f"미도착 shipped {shipped}건 = 민감도 4(D26)",
    )


def c3_carrier_before_approved(cur, s):
    total, in_period = one(
        cur,
        f"""SELECT count(*), count(*) FILTER (WHERE {PERIOD})
        FROM {s}.orders o
        WHERE o.order_delivered_carrier_date::timestamp <= o.order_approved_at::timestamp - interval '1 hour'""",
    )
    return Result(
        "C3",
        "택배 인계가 결제 승인보다 1시간 이상 앞섬",
        "report",
        "report",
        total,
        {"전체": total, "분석 기간": in_period},
    )


def c4_customer_before_carrier(cur, s):
    total, k = one(
        cur,
        f"""SELECT count(*), count(*) FILTER (WHERE {SCOPE})
        FROM {s}.orders o
        WHERE o.order_delivered_customer_date::timestamp <= o.order_delivered_carrier_date::timestamp - interval '1 hour'""",
    )
    n = late_group_size(cur, s)
    limit = gate_threshold(n)
    return Result(
        "C4",
        "도착이 택배 인계보다 1시간 이상 앞섬",
        "gate",
        "fail" if k >= limit else "pass",
        k,
        {"전체": total, "분석 범위": k, "실패 기준(건 이상)": limit},
        "D10 'C1과 같은 규칙' → D25 기준 적용",
    )


def c5_batch_closed_long(cur, s):
    hours, k = one(
        cur,
        f"""
        WITH o AS (
          SELECT o.order_id, date_trunc('hour', o.order_delivered_customer_date::timestamp) AS hr,
                 extract(epoch FROM o.order_delivered_customer_date::timestamp
                                    - o.order_purchase_timestamp::timestamp) / 86400 AS days
          FROM {s}.orders o
          WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL AND {PERIOD}
        ), h AS (SELECT hr FROM o WHERE days > 60 GROUP BY hr HAVING count(*) >= 3)
        SELECT (SELECT count(*) FROM h), count(*) FROM o WHERE days > 60 AND hr IN (SELECT hr FROM h)""",
    )
    return Result(
        "C5",
        "같은 1시간에 60일 넘은 주문 3건 이상 도착 처리",
        "report",
        "report",
        k,
        {"시간대 수": hours, "표시 주문": k},
        "민감도 1에서 제외",
    )


def c6_no_category(cur, s):
    products = one(
        cur, f"SELECT count(*) FROM {s}.products WHERE product_category_name IS NULL"
    )[0]
    orders = one(
        cur,
        f"""SELECT count(DISTINCT o.order_id) FROM {s}.orders o
        JOIN {s}.order_items i USING (order_id) JOIN {s}.products p USING (product_id)
        WHERE p.product_category_name IS NULL AND o.order_status = 'delivered' AND {PERIOD}""",
    )[0]
    return Result(
        "C6",
        "상품 카테고리 없음",
        "report",
        "report",
        products,
        {"상품": products, "영향 주문(분석 기간 delivered)": orders},
    )


def c7_lighter_than_air(cur, s):
    k = one(
        cur,
        f"""SELECT count(*) FROM {s}.products
        WHERE product_weight_g::numeric / NULLIF(product_length_cm::numeric * product_height_cm::numeric
                                                  * product_width_cm::numeric, 0) < 0.0012""",
    )[0]
    return Result("C7", "밀도가 공기(0.0012 g/cm³)보다 낮음", "report", "report", k)


def c8_payment_le_zero(cur, s):
    neg, zero = one(
        cur,
        f"""SELECT count(*) FILTER (WHERE payment_value::numeric < 0),
                                   count(*) FILTER (WHERE payment_value::numeric = 0)
                            FROM {s}.order_payments""",
    )
    return Result(
        "C8",
        "결제액 ≤ 0",
        "report",
        "report",
        neg + zero,
        {"음수": neg, "0원": zero, "ops_fail(음수)": neg},
    )


def c9_payment_mismatch(cur, s):
    inst_more, lump_more, less = one(
        cur,
        f"""
        WITH i AS (SELECT order_id, sum(price::numeric + freight_value::numeric) t
                   FROM {s}.order_items GROUP BY 1),
             p AS (SELECT order_id, sum(payment_value::numeric) t, max(payment_installments::int) inst
                   FROM {s}.order_payments GROUP BY 1),
             d AS (SELECT p.t - i.t AS diff, p.inst FROM i JOIN p USING (order_id))
        SELECT count(*) FILTER (WHERE diff > 0.01 AND inst > 1),
               count(*) FILTER (WHERE diff > 0.01 AND inst <= 1),
               count(*) FILTER (WHERE diff < -0.01)
        FROM d""",
    )
    return Result(
        "C9",
        "상품가+배송비 ≠ 결제 합(허용 R$0.01)",
        "report",
        "report",
        inst_more + lump_more + less,
        {
            "할부+더 냄": inst_more,
            "일시불+더 냄": lump_more,
            "덜 냄": less,
            "ops_fail(설명 안 됨)": lump_more + less,
        },
    )


def c10_orders_without_items(cur, s):
    by = dict(
        rows(
            cur,
            f"""SELECT o.order_status, count(*) FROM {s}.orders o
        LEFT JOIN (SELECT DISTINCT order_id FROM {s}.order_items) i USING (order_id)
        WHERE i.order_id IS NULL GROUP BY 1""",
        )
    )
    ops = sum(by.get(st, 0) for st in ("shipped", "invoiced", "delivered"))
    return Result(
        "C10",
        "품목 없는 주문",
        "report",
        "report",
        sum(by.values()),
        {**by, "ops_fail(배송·청구 단계)": ops},
    )


def c11_review_order_not_one_to_one(cur, s):
    shared, multi = one(
        cur,
        f"""SELECT
        (SELECT count(*) FROM (SELECT review_id FROM {s}.order_reviews GROUP BY 1
                               HAVING count(DISTINCT order_id) > 1) a),
        (SELECT count(*) FROM (SELECT order_id FROM {s}.order_reviews GROUP BY 1 HAVING count(*) > 1) b)""",
    )
    mixed_a, mixed_c, mixed_sp, reviews = one(
        cur,
        f"""WITH {analysis_base_cte(s)}
        SELECT count(*) FILTER (WHERE all_late <> any_late),
               count(*) FILTER (WHERE min_bin <> max_bin),
               count(*) FILTER (WHERE min_days <> max_days),
               count(*) FROM per_review""",
    )
    return Result(
        "C11",
        "리뷰와 주문이 일대일이 아님",
        "report",
        "report",
        shared + multi,
        {
            "공유 리뷰": shared,
            "리뷰 여러 개인 주문": multi,
            "분석 단위 리뷰": reviews,
            "(가) 제외": mixed_a,
            "(다) 제외": mixed_c,
            "Spearman 제외": mixed_sp,
        },
        "관문 조건 미정",
    )


def c12_geo_duplicates(cur, s):
    cur.execute(
        "SET LOCAL work_mem = '256MB'"
    )  # 100만 행 DISTINCT가 디스크로 넘치지 않게
    total, distinct_rows, prefixes = one(
        cur,
        f"""SELECT count(*),
        (SELECT count(*) FROM (SELECT DISTINCT * FROM {s}.geolocation) d),
        count(DISTINCT geolocation_zip_code_prefix) FROM {s}.geolocation""",
    )
    missing = one(
        cur,
        f"""SELECT count(*) FROM (SELECT DISTINCT customer_zip_code_prefix z FROM {s}.customers) c
        LEFT JOIN (SELECT DISTINCT geolocation_zip_code_prefix z FROM {s}.geolocation) g USING (z)
        WHERE g.z IS NULL""",
    )[0]
    return Result(
        "C12",
        "위치 테이블 완전 중복 행",
        "report",
        "report",
        total - distinct_rows,
        {
            "행": total,
            "완전 중복": total - distinct_rows,
            "앞자리": prefixes,
            "위치 없는 고객 앞자리": missing,
        },
    )


def c13_geo_outside_brazil(cur, s):
    k = one(
        cur,
        f"""SELECT count(*) FROM {s}.geolocation
        WHERE geolocation_lat::float NOT BETWEEN -33.75 AND 5.27
           OR geolocation_lng::float NOT BETWEEN -73.99 AND -28.8""",
    )[0]
    return Result("C13", "좌표가 브라질(섬 포함) 상자 밖", "report", "report", k)


def c14_canceled_with_date(cur, s):
    total, in_period = one(
        cur,
        f"""SELECT count(*), count(*) FILTER (WHERE {PERIOD}) FROM {s}.orders o
        WHERE o.order_status = 'canceled' AND o.order_delivered_customer_date IS NOT NULL""",
    )
    return Result(
        "C14",
        "canceled인데 도착일 있음",
        "report",
        "report",
        total,
        {"전체": total, "분석 기간": in_period},
    )


def c15_approval_delay(cur, s):
    """구매 50건 이상인 날, 그날 구매의 승인 지연 중앙값. 승인 기록이 없는 주문은 아주 큰 값(1e9시간)으로 센다
    (실시간 형태 = 구매 후 X시간이 지나도 절반 이상 미승인).

    중앙값 = 보간형(percentile_cont). 11-13 조사 숫자(3시간 26일·12시간 15일)와 같은 정의다.
    비보간형(percentile_disc)이면 2017-01-27이 11.998시간이 되어 12시간 이상이 14일로 바뀐다."""
    warn, fail = one(
        cur,
        f"""
        WITH d AS (
          SELECT o.order_purchase_timestamp::date AS day,
                 coalesce(extract(epoch FROM o.order_approved_at::timestamp
                                             - o.order_purchase_timestamp::timestamp) / 3600,
                          1e9)::float AS h
          FROM {s}.orders o WHERE {PERIOD}
        ), m AS (
          SELECT day, percentile_cont(0.5) WITHIN GROUP (ORDER BY h) AS med
          FROM d GROUP BY day HAVING count(*) >= 50
        )
        SELECT count(*) FILTER (WHERE med >= 3), count(*) FILTER (WHERE med >= 12) FROM m""",
    )
    return Result(
        "C15",
        "승인 지연 중앙값(구매 50건 이상인 날)",
        "report",
        "report",
        warn,
        {"3시간 이상(ops_warn)": warn, "12시간 이상(ops_fail)": fail},
    )


def c17_unmapped_category(cur, s):
    with (REFERENCE / "category_translation_extra.csv").open(encoding="utf-8") as f:
        extra = [r["product_category_name"] for r in csv.DictReader(f)]
    missing = rows(
        cur,
        f"""SELECT DISTINCT p.product_category_name FROM {s}.products p
        WHERE p.product_category_name IS NOT NULL
          AND p.product_category_name NOT IN (SELECT product_category_name
                                              FROM {s}.product_category_name_translation)
          AND NOT (p.product_category_name = ANY(%s))
        ORDER BY 1""",
        (extra,),
    )
    names = [r[0] for r in missing]
    return Result(
        "C17",
        "번역표·보충표에 없는 카테고리",
        "gate",
        "fail" if names else "pass",
        len(names),
        {"없는 이름": names} if names else {},
    )


def c18_estimated_date(cur, s):
    null, with_time, before = one(
        cur,
        f"""SELECT
        count(*) FILTER (WHERE o.order_estimated_delivery_date IS NULL),
        count(*) FILTER (WHERE o.order_estimated_delivery_date::timestamp
                               <> o.order_estimated_delivery_date::date::timestamp),
        count(*) FILTER (WHERE o.order_estimated_delivery_date::date < o.order_purchase_timestamp::date)
        FROM {s}.orders o WHERE o.order_status = 'delivered' AND {PERIOD}""",
    )
    total = null + with_time + before
    return Result(
        "C18",
        "예상 도착일 품질(분석 기간 delivered)",
        "gate",
        "fail" if total else "pass",
        total,
        {"빈 값": null, "시각 있음": with_time, "구매일보다 앞섬": before},
    )


ALL = [
    c0_types,
    c1_delivered_no_date,
    c2_no_date_by_status,
    c3_carrier_before_approved,
    c4_customer_before_carrier,
    c5_batch_closed_long,
    c6_no_category,
    c7_lighter_than_air,
    c8_payment_le_zero,
    c9_payment_mismatch,
    c10_orders_without_items,
    c11_review_order_not_one_to_one,
    c12_geo_duplicates,
    c13_geo_outside_brazil,
    c14_canceled_with_date,
    c15_approval_delay,
    c17_unmapped_category,
    c18_estimated_date,
]
