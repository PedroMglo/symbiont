INSERT INTO archives
                (archive_id, store_id, tier, backend, storage_target, archive_path, manifest_path,
                 summary_path, filelist_path, verify_path, original_size_bytes, archive_size_bytes,
                 reduction_ratio, files_count, created_at, verified, effective_config_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
