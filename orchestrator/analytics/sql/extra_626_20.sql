SELECT
                count() as total_runs,
                round(avg(total_duration_ms), 1) as avg_duration_ms,
                quantile(0.95)(total_duration_ms) as p95_duration_ms,
                countIf(success = 0) as error_runs,
                round(avg(node_count), 1) as avg_node_count,
                countIf(fallback_used = 1) as fallback_runs,
                countIf(critic_invoked = 1) as critic_runs,
                sum(critic_loops) as total_critic_loops,
                sum(total_tokens) as sum_tokens
            FROM graph_runs
            WHERE timestamp >= '{0}'
