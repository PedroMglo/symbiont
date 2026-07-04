SELECT
                intent,
                COUNT(*) as total_queries,
                GROUP_CONCAT(DISTINCT model) as models_used
            FROM llm_call_log WHERE timestamp >= ?
            GROUP BY intent ORDER BY total_queries DESC
