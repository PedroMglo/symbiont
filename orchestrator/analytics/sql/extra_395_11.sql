SELECT
                error_type,
                backend,
                count() as count,
                max(timestamp) as last_seen
            FROM llm_events
            WHERE timestamp >= '{0}' AND success = 0
            GROUP BY error_type, backend
            ORDER BY count DESC
