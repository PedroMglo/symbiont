SELECT job_id, kind, status, created_at, started_at, completed_at,
                       error, outputs_json, summary_json
                FROM jobs WHERE job_id = ?
