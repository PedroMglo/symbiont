UPDATE agentic_tasks
                    SET status = ?, updated_at = ?, metadata_json = ?
                    WHERE id = ? AND status = ?
