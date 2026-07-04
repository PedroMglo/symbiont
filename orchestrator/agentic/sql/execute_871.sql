INSERT INTO agentic_steps (
                    id, task_id, run_id, step_name, step_type, status, started_at,
                    finished_at, duration_ms, input_preview, output_preview, error_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
