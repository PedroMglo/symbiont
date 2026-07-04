INSERT INTO chunks
                    (chunk_id, doc_id, chunk_hash, text_ref, token_count, source_type,
                     page_start, page_end, heading_path_json, embedding_policy,
                     payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
