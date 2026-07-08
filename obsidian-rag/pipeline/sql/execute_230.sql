SELECT run_id FROM ingest_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1
