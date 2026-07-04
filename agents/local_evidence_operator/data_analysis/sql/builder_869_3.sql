{0}
SELECT (SELECT COUNT(*) FROM {1} b {2}) AS parent_rows_in_scope, (SELECT COUNT(*) FROM eligible_rows) AS eligible_rows, (SELECT COUNT(*) FROM {3} b {4}) AS parent_rows_not_eligible
