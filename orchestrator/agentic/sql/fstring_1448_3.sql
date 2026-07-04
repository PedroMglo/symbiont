SELECT * FROM agentic_approvals
            WHERE action = ? AND payload_hash = ? AND status IN ({0}) {1}
            ORDER BY requested_at DESC
            LIMIT 1
