SELECT * FROM agentic_improvement_proposals
            WHERE fingerprint = ? AND status IN (?, ?, ?)
            ORDER BY created_at DESC
            LIMIT 1
