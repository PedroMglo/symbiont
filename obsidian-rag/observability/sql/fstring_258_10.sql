SELECT
            timestamp, run_id,
            if(success = 1, 'ok', 'error') as status,
            files_scanned, files_parsed, files_skipped,
            chunks_produced, chunks_embedded, chunks_stored,
            stale_deleted, round(latency_ms / 1000, 1) as duration_s,
            error_count
        FROM rag_ingest_runs
        WHERE event = 'ingest_run_completed'
            AND timestamp > now() - INTERVAL {0} DAY
        ORDER BY timestamp DESC
        LIMIT 20
