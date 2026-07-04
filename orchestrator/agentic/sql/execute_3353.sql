UPDATE agentic_tasks
                SET status = ?, updated_at = ?
                WHERE status IN (?, ?)
