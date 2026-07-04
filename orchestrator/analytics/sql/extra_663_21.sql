SELECT
                graph_run_id,
                timestamp,
                session_id,
                total_duration_ms,
                node_count,
                success,
                intent,
                complexity,
                model_used,
                fallback_used,
                critic_invoked,
                path,
                agents_invoked,
                context_sources,
                total_tokens
            FROM graph_runs
            WHERE timestamp >= '{0}'
            ORDER BY timestamp DESC
            LIMIT {1}
