SELECT
            toStartOfHour(timestamp) as time,
            round(avg(best_score), 3) as avg_score,
            round(quantile(0.25)(best_score), 3) as p25_score,
            round(quantile(0.75)(best_score), 3) as p75_score,
            round(countIf(gate_passed = 1) * 100.0 / greatest(count(), 1), 1) as acceptance_rate
        FROM rag_retrieval
        WHERE timestamp > now() - INTERVAL {0} DAY AND best_score > 0
        GROUP BY time
        ORDER BY time
