SELECT COUNT(*) AS groups_count, COALESCE(SUM(cnt), 0) AS duplicate_rows FROM (SELECT {0} AS k, COUNT(*) AS cnt FROM {1} WHERE {2} IS NOT NULL GROUP BY {3} HAVING COUNT(*) > 1)
