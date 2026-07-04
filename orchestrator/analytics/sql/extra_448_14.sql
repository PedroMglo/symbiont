SELECT
                multiIf(
                    total_latency_ms < 1000, '<1s',
                    total_latency_ms < 2000, '1-2s',
                    total_latency_ms < 5000, '2-5s',
                    total_latency_ms < 10000, '5-10s',
                    total_latency_ms < 20000, '10-20s',
                    total_latency_ms < 50000, '20-50s',
                    '>50s'
                ) as range,
                count() as count
            FROM llm_events
            WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed') AND success = 1
            GROUP BY range
            ORDER BY min(total_latency_ms)
