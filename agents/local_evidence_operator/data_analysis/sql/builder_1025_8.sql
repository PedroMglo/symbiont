WITH left_counts AS (
  SELECT {0} AS k, COUNT(*) AS left_count FROM {1} GROUP BY {2}
), right_counts AS (
  SELECT {3} AS k, COUNT(*) AS right_count FROM {4} GROUP BY {5}
)
SELECT k, left_count, right_count, left_count * right_count AS join_rows
FROM left_counts JOIN right_counts USING(k)
WHERE left_count > 1 OR right_count > 1
ORDER BY join_rows DESC;
