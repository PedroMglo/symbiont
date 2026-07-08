INSERT INTO storage_upload_sessions
                (upload_id, object_id, version_id, store_id, created_by, created_at, updated_at,
                 expires_at, zone, status, policy, logical_name, content_type, temp_path, final_path,
                 expected_size, received_size, hash_algo, expected_hash, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
