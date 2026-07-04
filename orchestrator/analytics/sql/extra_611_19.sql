SELECT *
            FROM resource_samples
            WHERE timestamp >= now() - INTERVAL {0} MINUTE
            ORDER BY timestamp
