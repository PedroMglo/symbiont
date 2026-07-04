SELECT
                node_name,
                node_type,
                timestamp,
                duration_ms,
                success,
                error_type,
                error_message,
                tokens_used,
                parallel_group,
                input_keys,
                output_keys
            FROM graph_node_events
            WHERE graph_run_id = '{0}'
            ORDER BY timestamp ASC
