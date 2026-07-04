INSERT INTO agentic_command_runs (
                    id, session_id, task_id, trace_id, command, cwd, context_profile,
                    action, risk_level, policy_decision, status, exit_code,
                    stdout_preview, stderr_preview, output_truncated, started_at,
                    finished_at, duration_ms, approval_id, error, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
