SELECT
                session_id,
                COUNT(*) as queries,
                GROUP_CONCAT(DISTINCT model) as models_used,
                COALESCE(SUM(total_tokens), 0) as total_tokens,
                MIN(timestamp) as first_query_at,
                MAX(timestamp) as last_query_at
            FROM llm_call_log
            WHERE timestamp >= ? AND session_id IS NOT NULL
            GROUP BY session_id ORDER BY last_query_at DESC
            LIMIT ?
