-- 11-13 대화에서 stdlib로 센 후보 숫자를 SQL로 다시 잰다(정본).
-- 이 단계는 "무엇이 이상한가" 후보만 센다. 기준·처리는 docs/CRITERIA.md에서 정한다.
SELECT 'orders' AS item, count(*)::text AS n FROM raw.orders
UNION ALL SELECT 'orders_period', min(order_purchase_timestamp) || ' ~ ' || max(order_purchase_timestamp) FROM raw.orders
UNION ALL SELECT 'no_delivered_customer_date', count(*)::text FROM raw.orders WHERE order_delivered_customer_date IS NULL
UNION ALL SELECT 'delivered_status_but_no_date', count(*)::text FROM raw.orders WHERE order_status='delivered' AND order_delivered_customer_date IS NULL
UNION ALL SELECT 'product_no_category', count(*)::text FROM raw.products WHERE product_category_name IS NULL
UNION ALL SELECT 'geo_exact_dup_rows', (count(*) - (SELECT count(*) FROM (SELECT DISTINCT * FROM raw.geolocation) d))::text FROM raw.geolocation
UNION ALL SELECT 'review_id_repeated', count(*)::text FROM (SELECT review_id FROM raw.order_reviews GROUP BY 1 HAVING count(*)>1) r
UNION ALL SELECT 'carrier_before_approved', count(*)::text FROM raw.orders WHERE order_delivered_carrier_date::timestamp < order_approved_at::timestamp
UNION ALL SELECT 'customer_before_carrier', count(*)::text FROM raw.orders WHERE order_delivered_customer_date::timestamp < order_delivered_carrier_date::timestamp
UNION ALL SELECT 'weight_zero', count(*)::text FROM raw.products WHERE product_weight_g::numeric = 0
UNION ALL SELECT 'payment_le_zero', count(*)::text FROM raw.order_payments WHERE payment_value::numeric <= 0
UNION ALL SELECT 'delivery_days_max_median',
  max(d)::text || ' / ' || percentile_cont(0.5) WITHIN GROUP (ORDER BY d)::text
  FROM (SELECT extract(epoch FROM order_delivered_customer_date::timestamp - order_purchase_timestamp::timestamp)/86400 AS d
        FROM raw.orders WHERE order_delivered_customer_date IS NOT NULL) x
UNION ALL SELECT 'geo_outside_brazil_box', count(*)::text FROM raw.geolocation
  WHERE geolocation_lat::float NOT BETWEEN -33.75 AND 5.27 OR geolocation_lng::float NOT BETWEEN -73.99 AND -34.79
UNION ALL SELECT 'items_total_ne_payment_total', count(*)::text FROM (
  SELECT i.order_id FROM (SELECT order_id, sum(price::numeric + freight_value::numeric) t FROM raw.order_items GROUP BY 1) i
  JOIN (SELECT order_id, sum(payment_value::numeric) t FROM raw.order_payments GROUP BY 1) p USING (order_id)
  WHERE abs(i.t - p.t) > 0.01) z
UNION ALL SELECT 'orders_without_items', count(*)::text FROM raw.orders o WHERE NOT EXISTS (SELECT 1 FROM raw.order_items i WHERE i.order_id=o.order_id);
