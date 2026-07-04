UPDATE agentic_resource_leases
                SET status = ?
                WHERE released_at IS NULL AND expires_at IS NOT NULL AND expires_at < ? AND status != ?
