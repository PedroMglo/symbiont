INSERT INTO storage_objects
                (object_id, latest_version_id, store_id, created_by, created_at, updated_at, purpose, zone, status,
                 policy, current_path, relative_path, absolute_path_hash, size_bytes, hash_algo,
                 content_hash, source_file, source_content_hash, parent_object_id, model, logical_name, content_type, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
