SELECT reason, COUNT(*) AS count FROM storage_control_events WHERE allowed = 0 GROUP BY reason
