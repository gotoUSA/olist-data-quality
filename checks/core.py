"""검사 공통 부품: 결과 형식, 분석 범위, C1·C4 실패 기준 계산.

기준과 이유는 docs/CRITERIA.md, 결정 경위는 docs/DECISIONS.md가 정본이다.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field

DSN = os.environ.get("OLIST_DSN", "postgresql://olist:olist@localhost:5440/olist")

# 분석 기간(D2): 2017-01-01 ~ 2018-08-31 구매, 그중 예상 도착일 ≤ 2018-08-31
PERIOD = (
    "o.order_purchase_timestamp::timestamp >= '2017-01-01' "
    "AND o.order_purchase_timestamp::timestamp < '2018-09-01'"
)
EST_OK = "o.order_estimated_delivery_date::date <= '2018-08-31'"
SCOPE = f"{PERIOD} AND {EST_OK}"

# 보고 자릿수(D25): 평균 소수 첫째 자리, 비율 정수 % → 허용 폭은 반올림 단위의 절반
MEAN_TOL = 0.05  # 점
RATE_TOL = 0.005  # 0.5%p
SCORE_RANGE = 4  # 리뷰 1~5점


@dataclass
class Result:
    id: str
    title: str
    role: str  # "gate" = 분석 관문(실패하면 분석 중단), "report" = 보고만
    status: str  # "pass" | "fail" | "report"
    count: int
    details: dict = field(default_factory=dict)
    note: str = ""


def check_schema(schema: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
        raise ValueError(f"schema name not allowed: {schema!r}")
    return schema


def analysis_base_cte(s: str) -> str:
    """sql/analysis_set.sql과 같은 분석 단위. 리뷰 점수는 고르지도 집계하지도 않는다."""
    return f"""
    base AS (
      SELECT o.order_id,
             o.order_delivered_customer_date::date - o.order_estimated_delivery_date::date AS late_days
      FROM {s}.orders o
      WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
        AND {SCOPE}
    ), last_review AS (
      SELECT DISTINCT ON (order_id) order_id, review_id
      FROM {s}.order_reviews
      ORDER BY order_id, review_creation_date::timestamp DESC,
               review_answer_timestamp::timestamp DESC, review_id
    ), unit AS (
      SELECT b.order_id, b.late_days, r.review_id, (b.late_days >= 1) AS is_late,
             CASE WHEN b.late_days < 0 THEN 1 WHEN b.late_days = 0 THEN 2
                  WHEN b.late_days <= 3 THEN 3 WHEN b.late_days <= 7 THEN 4 ELSE 5 END AS late_bin
      FROM base b JOIN last_review r USING (order_id)
    ), per_review AS (
      SELECT review_id, bool_and(is_late) AS all_late, bool_or(is_late) AS any_late,
             min(late_bin) AS min_bin, max(late_bin) AS max_bin,
             min(late_days) AS min_days, max(late_days) AS max_days
      FROM unit GROUP BY review_id
    )"""


def late_group_size(cur, s: str) -> int:
    """분석 단위 (가)의 늦은 리뷰 수 n(D25). 묶인 주문끼리 늦음 여부가 갈리는 리뷰는 뺀다(D23)."""
    cur.execute(
        f"WITH {analysis_base_cte(s)} SELECT count(*) FROM per_review WHERE all_late"
    )
    return cur.fetchone()[0]


def gate_threshold(n: int) -> int:
    """최악의 경우(k건이 모두 늦은 집단에 1점 또는 5점으로 들어감) 보고 숫자가
    반올림 한 칸 넘게 움직이기 시작하는 가장 작은 k(D25).

    1~2점 비율 이동 k/(n+k) > RATE_TOL, 평균 이동 4k/(n+k) > MEAN_TOL 중 먼저 넘는 쪽.
    """
    k_rate = math.floor(RATE_TOL * n / (1 - RATE_TOL)) + 1
    k_mean = math.floor(MEAN_TOL * n / (SCORE_RANGE - MEAN_TOL)) + 1
    return min(k_rate, k_mean)


def one(cur, sql: str, params=None):
    cur.execute(sql, params)
    return cur.fetchone()


def rows(cur, sql: str, params=None):
    cur.execute(sql, params)
    return cur.fetchall()
