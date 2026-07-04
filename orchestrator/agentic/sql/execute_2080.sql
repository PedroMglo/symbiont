UPDATE agentic_preapproval_windows
                SET status = ?, revoked_at = ?, revoked_reason = ?
                WHERE id = ?
