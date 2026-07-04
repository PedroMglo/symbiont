SELECT
                requested_model,
                model as resolved_model,
                fallback_reason,
                COUNT(*) as count
            FROM llm_call_log
            WHERE timestamp >= ? AND fallback_used = 1
            GROUP BY requested_model, model, fallback_reason
            ORDER BY count DESC
