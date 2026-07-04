UPDATE agentic_tasks
                SET status = ?, updated_at = ?, result_json = ?, error_json = ?, metadata_json = ?
                WHERE id = ?
