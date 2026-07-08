INSERT INTO files
                (file_id, store_id, relative_path, absolute_path_hash, extension, size_bytes,
                 modified_at, accessed_at, processed_at, effective_age_days, hash_algo,
                 content_hash, detected_type, lifecycle_state, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
