"""오류 주입 테스트: raw를 inj 스키마로 복사하고, 테스트마다 오류를 넣은 뒤 검사가
넣은 만큼 정확히 잡는지(그리고 허용 범위 안의 값은 잡지 않는지) 확인한다.

각 테스트는 한 트랜잭션 안에서 돌고 끝나면 되돌린다(inj 스키마는 테스트 사이에 바뀌지 않는다).
실행: .venv/Scripts/python -m pytest tests -q   (DB: docker compose up -d)
"""

from __future__ import annotations

import psycopg
import pytest

from checks import catalog as C
from checks.core import DSN, SCOPE, gate_threshold, late_group_size

S = "inj"
TABLES = [
    "orders",
    "order_items",
    "order_payments",
    "order_reviews",
    "products",
    "product_category_name_translation",
    "customers",
    "geolocation",
]


@pytest.fixture(scope="session")
def schema():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")
        conn.execute(f"CREATE SCHEMA {S}")
        for t in TABLES:
            conn.execute(f"CREATE TABLE {S}.{t} AS TABLE raw.{t}")
            conn.execute(
                f"VACUUM ANALYZE {S}.{t}"
            )  # 통계 없이 돌면 큰 집계가 디스크로 넘침
    yield S
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {S} CASCADE")


@pytest.fixture
def cur(schema):
    conn = psycopg.connect(DSN)
    try:
        with conn.cursor() as c:
            yield c
    finally:
        conn.rollback()
        conn.close()


def ids(cur, sql, n):
    cur.execute(sql + f" LIMIT {n}")
    got = [r[0] for r in cur.fetchall()]
    assert len(got) == n
    return got


# 분석 범위 안의 delivered 주문 중 예상일보다 일찍 온 주문(늦은 집단 n을 바꾸지 않음)
EARLY = f"""SELECT o.order_id FROM {S}.orders o
    WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL AND {SCOPE}
      AND o.order_delivered_customer_date::date < o.order_estimated_delivery_date::date - 3
      AND o.order_delivered_carrier_date::timestamp > o.order_approved_at::timestamp + interval '1 day'
    ORDER BY o.order_id"""


def test_c0_bad_date_fails_gate(cur):
    oid = ids(cur, EARLY, 1)[0]
    cur.execute(
        f"UPDATE {S}.orders SET order_estimated_delivery_date = '2018-02-30' WHERE order_id = %s",
        (oid,),
    )
    r = C.c0_types(cur, S)
    assert r.status == "fail"
    assert r.details == {"orders.order_estimated_delivery_date": 1}


def test_c1_threshold_boundary(cur):
    base = C.c1_delivered_no_date(cur, S)
    assert base.status == "pass"
    limit = base.details["실패 기준(건 이상)"]
    targets = ids(cur, EARLY, limit - base.count)
    # 기준 한 건 아래까지: 통과
    cur.execute(
        f"UPDATE {S}.orders SET order_delivered_customer_date = NULL WHERE order_id = ANY(%s)",
        (targets[:-1],),
    )
    below = C.c1_delivered_no_date(cur, S)
    assert below.count == limit - 1 and below.status == "pass"
    # 한 건 더: 실패
    cur.execute(
        f"UPDATE {S}.orders SET order_delivered_customer_date = NULL WHERE order_id = %s",
        (targets[-1],),
    )
    at = C.c1_delivered_no_date(cur, S)
    assert at.count == limit and at.status == "fail"
    assert (
        at.details["늦은 리뷰 n"] == base.details["늦은 리뷰 n"]
    )  # 일찍 온 주문만 건드림


def test_gate_threshold_matches_d25():
    assert gate_threshold(6325) == 32  # D25: 0.005n/0.995 = 31.8 → 32건부터 실패
    assert gate_threshold(0) == 1


def test_c2_shipped_counted(cur):
    base = C.c2_no_date_by_status(cur, S).details["shipped"]
    targets = ids(cur, EARLY, 3)
    cur.execute(
        f"""UPDATE {S}.orders SET order_status = 'shipped', order_delivered_customer_date = NULL
                    WHERE order_id = ANY(%s)""",
        (targets,),
    )
    assert C.c2_no_date_by_status(cur, S).details["shipped"] == base + 3


def test_c3_one_hour_tolerance(cur):
    base = C.c3_carrier_before_approved(cur, S).count
    a, b = ids(cur, EARLY, 2)
    cur.execute(
        f"""UPDATE {S}.orders SET order_delivered_carrier_date =
                    (order_approved_at::timestamp - interval '30 minutes')::text WHERE order_id = %s""",
        (a,),
    )
    cur.execute(
        f"""UPDATE {S}.orders SET order_delivered_carrier_date =
                    (order_approved_at::timestamp - interval '2 hours')::text WHERE order_id = %s""",
        (b,),
    )
    assert (
        C.c3_carrier_before_approved(cur, S).count == base + 1
    )  # 30분 = 시계 오차, 2시간만 셈


def test_c4_tolerance_and_threshold(cur):
    base = C.c4_customer_before_carrier(cur, S)
    assert base.status == "pass"
    limit = base.details["실패 기준(건 이상)"]
    targets = ids(cur, EARLY, limit - base.count + 1)
    cur.execute(
        f"""UPDATE {S}.orders SET order_delivered_customer_date =
                    (order_delivered_carrier_date::timestamp - interval '30 minutes')::text
                    WHERE order_id = %s""",
        (targets[0],),
    )
    assert C.c4_customer_before_carrier(cur, S).count == base.count  # 허용 범위
    cur.execute(
        f"""UPDATE {S}.orders SET order_delivered_customer_date =
                    (order_delivered_carrier_date::timestamp - interval '2 hours')::text
                    WHERE order_id = ANY(%s)""",
        (targets[1:],),
    )
    r = C.c4_customer_before_carrier(cur, S)
    assert r.count == limit and r.status == "fail"


def test_c5_batch_close_needs_three(cur):
    base = C.c5_batch_closed_long(cur, S).count
    old = ids(
        cur,
        f"""SELECT o.order_id FROM {S}.orders o WHERE o.order_status = 'delivered'
                       AND o.order_purchase_timestamp::timestamp BETWEEN '2017-03-01' AND '2017-06-30'
                       ORDER BY o.order_id""",
        3,
    )
    hour = "2018-03-10 03:20:00"  # 이 시간대에 60일 넘은 도착 처리가 원래 없음
    cur.execute(
        f"UPDATE {S}.orders SET order_delivered_customer_date = %s WHERE order_id = ANY(%s)",
        (hour, old[:2]),
    )
    assert C.c5_batch_closed_long(cur, S).count == base  # 2건은 몰아서 닫힘으로 안 봄
    cur.execute(
        f"UPDATE {S}.orders SET order_delivered_customer_date = %s WHERE order_id = %s",
        (hour, old[2]),
    )
    assert C.c5_batch_closed_long(cur, S).count == base + 3


def test_c6_null_category(cur):
    base = C.c6_no_category(cur, S).count
    pid = ids(
        cur,
        f"SELECT product_id FROM {S}.products WHERE product_category_name IS NOT NULL ORDER BY 1",
        1,
    )
    cur.execute(
        f"UPDATE {S}.products SET product_category_name = NULL WHERE product_id = %s",
        (pid[0],),
    )
    assert C.c6_no_category(cur, S).count == base + 1


def test_c7_zero_weight(cur):
    base = C.c7_lighter_than_air(cur, S).count
    pid = ids(
        cur,
        f"""SELECT product_id FROM {S}.products WHERE product_weight_g::numeric >= 500
                       AND product_length_cm::numeric > 0 ORDER BY 1""",
        1,
    )
    cur.execute(
        f"UPDATE {S}.products SET product_weight_g = '0' WHERE product_id = %s",
        (pid[0],),
    )
    assert C.c7_lighter_than_air(cur, S).count == base + 1


def test_c8_negative_payment(cur):
    cur.execute(f"""UPDATE {S}.order_payments SET payment_value = '-5.00'
                    WHERE ctid = (SELECT ctid FROM {S}.order_payments WHERE payment_value::numeric > 10 LIMIT 1)""")
    assert C.c8_payment_le_zero(cur, S).details["ops_fail(음수)"] == 1


# 결제 1건·일시불이고 상품가+배송비와 정확히 같은 주문
MATCHED = f"""SELECT p.order_id FROM {S}.order_payments p
    JOIN (SELECT order_id, sum(price::numeric + freight_value::numeric) t FROM {S}.order_items GROUP BY 1) i
      USING (order_id)
    WHERE p.payment_installments = '1'
    GROUP BY p.order_id, i.t HAVING count(*) = 1 AND sum(p.payment_value::numeric) = i.t
    ORDER BY 1"""


def test_c9_groups(cur):
    base = C.c9_payment_mismatch(cur, S).details
    a, b, c, d = ids(cur, MATCHED, 4)
    cur.execute(
        f"UPDATE {S}.order_payments SET payment_value = (payment_value::numeric + 5)::text WHERE order_id = %s",
        (a,),
    )
    cur.execute(
        f"""UPDATE {S}.order_payments SET payment_value = (payment_value::numeric + 5)::text,
                    payment_installments = '3' WHERE order_id = %s""",
        (b,),
    )
    cur.execute(
        f"UPDATE {S}.order_payments SET payment_value = (payment_value::numeric - 5)::text WHERE order_id = %s",
        (c,),
    )
    cur.execute(
        f"UPDATE {S}.order_payments SET payment_value = (payment_value::numeric + 0.01)::text WHERE order_id = %s",
        (d,),
    )
    got = C.c9_payment_mismatch(cur, S).details
    assert got["일시불+더 냄"] == base["일시불+더 냄"] + 1
    assert got["할부+더 냄"] == base["할부+더 냄"] + 1
    assert got["덜 냄"] == base["덜 냄"] + 1  # R$0.01 차이(d)는 허용 범위
    assert got["ops_fail(설명 안 됨)"] == base["ops_fail(설명 안 됨)"] + 2


def test_c10_delivered_without_items(cur):
    base = C.c10_orders_without_items(cur, S).details["ops_fail(배송·청구 단계)"]
    oid = ids(cur, EARLY, 1)[0]
    cur.execute(f"DELETE FROM {S}.order_items WHERE order_id = %s", (oid,))
    assert (
        C.c10_orders_without_items(cur, S).details["ops_fail(배송·청구 단계)"]
        == base + 1
    )


def test_c11_shared_review(cur):
    base = C.c11_review_order_not_one_to_one(cur, S).details["공유 리뷰"]
    src, dst = ids(
        cur,
        f"""SELECT order_id FROM {S}.order_reviews WHERE order_id IN (
                           SELECT order_id FROM {S}.order_reviews GROUP BY 1 HAVING count(*) = 1)
                           AND review_id IN (SELECT review_id FROM {S}.order_reviews GROUP BY 1
                                             HAVING count(*) = 1) ORDER BY 1""",
        2,
    )
    cur.execute(
        f"""INSERT INTO {S}.order_reviews
                    SELECT review_id, %s, review_score, review_comment_title, review_comment_message,
                           review_creation_date, review_answer_timestamp
                    FROM {S}.order_reviews WHERE order_id = %s""",
        (dst, src),
    )
    assert C.c11_review_order_not_one_to_one(cur, S).details["공유 리뷰"] == base + 1


def test_c12_duplicate_geo_row(cur):
    base = C.c12_geo_duplicates(cur, S).count
    cur.execute(f"""INSERT INTO {S}.geolocation SELECT * FROM {S}.geolocation
                    WHERE geolocation_zip_code_prefix = (SELECT geolocation_zip_code_prefix FROM {S}.geolocation
                                                         GROUP BY 1 HAVING count(*) = 1 ORDER BY 1 LIMIT 1)""")
    assert C.c12_geo_duplicates(cur, S).count == base + 1


def test_c13_other_continent_vs_island(cur):
    base = C.c13_geo_outside_brazil(cur, S).count
    cur.execute(f"""UPDATE {S}.geolocation SET geolocation_lat = '40.4', geolocation_lng = '-8.6'
                    WHERE ctid = (SELECT ctid FROM {S}.geolocation WHERE geolocation_state = 'SP' LIMIT 1)""")
    cur.execute(f"""UPDATE {S}.geolocation SET geolocation_lat = '-3.85', geolocation_lng = '-32.42'
                    WHERE ctid = (SELECT ctid FROM {S}.geolocation WHERE geolocation_state = 'RJ' LIMIT 1)""")
    assert (
        C.c13_geo_outside_brazil(cur, S).count == base + 1
    )  # 페르난두지노로냐 좌표는 안 셈


def test_c14_canceled_with_date(cur):
    base = C.c14_canceled_with_date(cur, S).count
    oid = ids(cur, EARLY, 1)[0]
    cur.execute(
        f"UPDATE {S}.orders SET order_status = 'canceled' WHERE order_id = %s", (oid,)
    )
    assert C.c14_canceled_with_date(cur, S).count == base + 1


def test_c15_stalled_day(cur):
    base = C.c15_approval_delay(cur, S).details
    day = ids(
        cur,
        f"""SELECT order_purchase_timestamp::date FROM {S}.orders
                       WHERE order_purchase_timestamp::date = '2018-03-14' GROUP BY 1 HAVING count(*) >= 50""",
        1,
    )[0]
    cur.execute(
        f"""UPDATE {S}.orders SET order_approved_at =
                    (order_purchase_timestamp::timestamp + interval '13 hours')::text
                    WHERE order_purchase_timestamp::date = %s""",
        (day,),
    )
    got = C.c15_approval_delay(cur, S).details
    assert got["12시간 이상(ops_fail)"] == base["12시간 이상(ops_fail)"] + 1
    assert got["3시간 이상(ops_warn)"] == base["3시간 이상(ops_warn)"] + 1


def test_c17_new_category_fails(cur):
    cur.execute(f"""UPDATE {S}.products SET product_category_name = 'categoria_nova'
                    WHERE product_id = (SELECT product_id FROM {S}.products ORDER BY 1 LIMIT 1)""")
    r = C.c17_unmapped_category(cur, S)
    assert r.status == "fail" and r.details["없는 이름"] == ["categoria_nova"]


def test_c18_estimate_before_purchase_fails(cur):
    oid = ids(cur, EARLY, 1)[0]
    cur.execute(
        f"""UPDATE {S}.orders SET order_estimated_delivery_date =
                    (order_purchase_timestamp::date - 1)::text WHERE order_id = %s""",
        (oid,),
    )
    r = C.c18_estimated_date(cur, S)
    assert r.status == "fail" and r.details["구매일보다 앞섬"] == 1


def test_clean_copy_passes_all_gates(cur):
    for fn in C.ALL:
        r = fn(cur, S)
        if r.role == "gate":
            assert r.status == "pass", r.id
