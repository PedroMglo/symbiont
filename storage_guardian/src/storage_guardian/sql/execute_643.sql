SELECT COALESCE(SUM(size_bytes), 0) FROM storage_objects WHERE status = 'active'
