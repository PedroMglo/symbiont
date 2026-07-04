SELECT
                node_name,
                node_type,
                count() as executions,
                round(avg(duration_ms), 1) as avg_ms,
                quantile(0.5)(duration_ms) as p50_ms,
                quantile(0.95)(duration_ms) as p95_ms,
                quantile(0.99)(duration_ms) as p99_ms,
                max(duration_ms) as max_ms,
                countIf(success = 0) as errors,
                sum(tokens_used) as sum_tokens
            FROM graph_node_events
            WHERE timestamp >= '{0}'
            GROUP BY node_name, node_type
            ORDER BY executions DESC
