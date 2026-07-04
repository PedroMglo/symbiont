INSERT INTO agentic_runs (
                    id, task_id, trace_id, graph_run_id, entrypoint, status,
                    started_at, finished_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
