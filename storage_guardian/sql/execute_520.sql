SELECT COALESCE(SUM(size_bytes), 0)
                FROM storage_objects
                WHERE created_by = ? AND status NOT IN ('quarantined', 'deleted')
