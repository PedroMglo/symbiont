SELECT * FROM agentic_actuations WHERE status IN (?, ?) AND expires_at IS NOT NULL AND expires_at < ?
