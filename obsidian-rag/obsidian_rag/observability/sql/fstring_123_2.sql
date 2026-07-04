SELECT
            count() as total_retrievals,
            round(countIf(gate_passed = 1) * 100.0 / greatest(count(), 1), 1) as acceptance_rate,
            round(avg(best_score), 3) as avg_best_score,
            round(avg(latency_ms), 1) as avg_retrieval_latency
        FROM rag_retrieval
        WHERE timestamp > now() - INTERVAL {0} DAY
