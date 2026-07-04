SELECT
            if(success = 1, 'ok', 'error') as last_status,
            chunks_stored as last_chunks,
            files_parsed as last_files
        FROM rag_ingest_runs
        WHERE event = 'ingest_run_completed'
        ORDER BY timestamp DESC LIMIT 1
