UPDATE agentic_resource_leases
                SET status = ?, renewed_at = ?, expires_at = COALESCE(?, expires_at)
                WHERE lease_id = ? AND released_at IS NULL
