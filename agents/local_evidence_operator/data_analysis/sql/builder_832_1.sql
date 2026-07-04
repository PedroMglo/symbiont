{0}
SELECT CAST(ROUND(COALESCE(SUM(recognized_cents * rate_to_base / 100.0), 0) - {1}) AS INTEGER) AS reconciled_total
FROM eligible_rows;
