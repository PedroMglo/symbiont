SELECT
                error_type,
                backend,
                COUNT(*) as count,
                MAX(timestamp) as last_seen
            FROM llm_call_log
            WHERE timestamp >= ? AND success = 0
            GROUP BY error_type, backend
            ORDER BY count DESC
