-- 분석 단위 만들기(11-14 결정). 리뷰 점수는 고르지도 집계하지도 않고, 어떤 행이 들어가는지만 정한다.
-- 1) 분석 기간 delivered 주문 중 예상 도착일이 마지막 리뷰일(2018-08-31) 이전인 주문(D2), 지연 일수와 구간
-- 2) 주문에 리뷰가 여러 개면 가장 나중에 쓴 리뷰 하나
-- 3) 리뷰 하나가 여러 주문에 붙으면: 늦음 여부가 같으면 1행, 갈리면 제외(가). 구간이 같으면 1행, 갈리면 제외(다).
DROP TABLE IF EXISTS analysis_base;
CREATE TABLE analysis_base AS
WITH o AS (
  SELECT o.order_id, c.customer_state,
         o.order_delivered_customer_date::date - o.order_estimated_delivery_date::date AS late_days
  FROM raw.orders o JOIN raw.customers c USING (customer_id)
  WHERE o.order_status = 'delivered' AND o.order_delivered_customer_date IS NOT NULL
    AND o.order_purchase_timestamp >= '2017-01-01' AND o.order_purchase_timestamp < '2018-09-01'
    AND o.order_estimated_delivery_date::date <= '2018-08-31'  -- 늦음으로 관측될 수 있었던 주문만
), r AS (
  SELECT DISTINCT ON (order_id) order_id, review_id
  FROM raw.order_reviews
  ORDER BY order_id, review_creation_date::timestamp DESC, review_answer_timestamp::timestamp DESC, review_id
)
SELECT o.*, r.review_id,
       (o.late_days >= 1) AS is_late,
       CASE WHEN o.late_days < 0 THEN 1 WHEN o.late_days = 0 THEN 2 WHEN o.late_days <= 3 THEN 3
            WHEN o.late_days <= 7 THEN 4 ELSE 5 END AS late_bin
FROM o LEFT JOIN r USING (order_id);
