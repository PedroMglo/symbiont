SELECT type, name, sql
FROM sqlite_master
WHERE type IN (?, ?)
  AND name NOT LIKE ?
ORDER BY type, name
