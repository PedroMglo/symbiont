SELECT
                {0} as period,
                count() as runs,
                round(avg(total_duration_ms), 1) as avg_duration_ms,
                countIf(success = 0) as errors,
                countIf(fallback_used = 1) as fallbacks
            FROM graph_runs
            WHERE timestamp >= '{1}'
            GROUP BY period
            ORDER BY period
