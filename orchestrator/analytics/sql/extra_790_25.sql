SELECT
                node_name,
                node_type,
                timestamp,
                duration_ms,
                graph_run_id,
                session_id,
                error_type
            FROM graph_node_events
            WHERE timestamp >= '{0}'
              AND duration_ms >= {1}
            ORDER BY duration_ms DESC
            LIMIT 50
