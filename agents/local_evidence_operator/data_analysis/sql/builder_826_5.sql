COALESCE((SELECT SUM(COALESCE(a.{0}, 0)) / 100.0 FROM {1} a JOIN eligible_rows e ON {2}), 0)
