SELECT
                requested_model,
                model as resolved_model,
                fallback_reason,
                count() as count
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
              AND fallback_used = 1
            GROUP BY requested_model, model, fallback_reason
            ORDER BY count DESC
