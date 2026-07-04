SELECT
                model,
                count() as queries,
                round(avg(total_latency_ms), 1) as avg_lat,
                min(total_latency_ms) as min_lat,
                max(total_latency_ms) as max_lat
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed') AND success = 1
            GROUP BY model ORDER BY avg_lat
