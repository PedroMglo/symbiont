SELECT doc_id FROM documents
                WHERE source_path = ?
                ORDER BY updated_at DESC
                LIMIT 1
