SELECT doc_id FROM documents
                WHERE file_hash = ? AND config_hash = ? AND status = 'completed'
                  AND (? IS NULL OR source_type = ?)
                ORDER BY updated_at DESC
                LIMIT 1
