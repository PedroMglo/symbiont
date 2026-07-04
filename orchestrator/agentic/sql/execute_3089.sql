UPDATE agentic_actuations
                SET status = ?, after_json = ?, updated_at = ?, rolled_back_at = ?, rollback_reason = ?
                WHERE id = ?
