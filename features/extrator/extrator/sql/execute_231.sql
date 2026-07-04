INSERT OR REPLACE INTO documents
                (doc_id, source_path, source_type, file_hash, config_hash, status,
                 output_paths_json, metadata_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
