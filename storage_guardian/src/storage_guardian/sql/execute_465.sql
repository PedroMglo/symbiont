INSERT INTO storage_object_versions
                (version_id, object_id, store_id, created_by, created_at, zone, status, policy,
                 logical_name, content_type, current_path, relative_path, size_bytes, hash_algo,
                 content_hash, parent_object_id, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
