SELECT
                count() as total_measured,
                quantile(0.50)(total_latency_ms) as p50,
                quantile(0.95)(total_latency_ms) as p95,
                quantile(0.99)(total_latency_ms) as p99,
                avg(total_latency_ms) as avg_lat,
                quantile(0.50)(first_token_latency_ms) as ft_p50
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed') AND success = 1
