SELECT doc_id FROM documents
                WHERE source_path = ? AND file_hash = ? AND config_hash = ? AND status = 'completed'
                LIMIT 1
