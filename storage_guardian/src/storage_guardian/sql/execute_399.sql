INSERT INTO restore_events
                (restore_id, archive_id, requested_by, restore_root, started_at, finished_at, verified, overwritten_existing_files)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
