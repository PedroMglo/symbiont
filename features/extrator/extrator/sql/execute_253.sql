SELECT doc_id, source_path, source_type, file_hash, status,
                       output_paths_json, metadata_json
                FROM documents WHERE doc_id = ?
