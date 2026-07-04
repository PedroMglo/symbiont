INSERT INTO agentic_tool_calls (
                    id, task_id, step_id, tool_name, risk_level, status, input_preview,
                    output_preview, started_at, finished_at, requires_approval,
                    approval_id, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
