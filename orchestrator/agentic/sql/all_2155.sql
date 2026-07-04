SELECT * FROM agentic_preapproval_windows
            WHERE action = ? AND status = ? AND expires_at >= ?
            ORDER BY created_at
