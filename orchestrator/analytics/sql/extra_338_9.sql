SELECT
                intent,
                count() as total_queries,
                groupUniqArray(model) as models_arr
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
            GROUP BY intent ORDER BY total_queries DESC
