SELECT
                session_id,
                count() as queries,
                groupUniqArray(model) as models_used_arr,
                sum(total_tokens) as sum_tok,
                min(timestamp) as first_query_at,
                max(timestamp) as last_query_at
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
              AND session_id != ''
            GROUP BY session_id ORDER BY last_query_at DESC
            LIMIT {1}
