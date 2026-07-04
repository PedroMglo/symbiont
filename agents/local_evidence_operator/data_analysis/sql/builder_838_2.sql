{0}
SELECT entity_id,
       ROUND(measure_cents / 100.0, 2) AS raw_measure,
       ROUND(excluded_tax_cents / 100.0, 2) AS excluded_tax,
       ROUND(recognized_cents * rate_to_base / 100.0, 2) AS recognized_value,
       rate_to_base,
       settlement_date,
       recognition_period
FROM eligible_rows
ORDER BY entity_id
LIMIT 50;
