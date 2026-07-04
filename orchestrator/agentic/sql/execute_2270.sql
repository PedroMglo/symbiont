UPDATE agentic_resource_leases
                SET status = ?, released_at = ?
                WHERE lease_id = ? AND released_at IS NULL
