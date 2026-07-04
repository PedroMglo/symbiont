INSERT INTO agentic_parallel_rounds (
                    id, task_id, trace_id, plan_id, status, plan_json,
                    round_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    plan_json = excluded.plan_json,
                    round_json = excluded.round_json,
                    updated_at = excluded.updated_at
