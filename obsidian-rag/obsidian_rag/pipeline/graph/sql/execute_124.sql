INSERT INTO extraction_cache
                       (file_hash, model_name, schema_version, graphify_version, graph_fragment, extracted_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_hash, model_name, schema_version, graphify_version) DO UPDATE SET
                     graph_fragment = excluded.graph_fragment,
                     extracted_at = excluded.extracted_at
