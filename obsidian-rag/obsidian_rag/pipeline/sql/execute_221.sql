UPDATE ingest_runs SET finished_at = datetime('now'), status = ?, error = ? WHERE run_id = ?
