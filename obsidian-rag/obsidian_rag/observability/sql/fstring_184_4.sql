SELECT
            count() as total,
            round(countIf(gate_passed = 1) * 100.0 / greatest(count(), 1), 1) as acceptance_rate,
            round(avg(best_score), 3) as avg_best_score,
            round(countIf(reranker_used = 1) * 100.0 / greatest(count(), 1), 1) as reranker_pct,
            round(countIf(hyde_used = 1) * 100.0 / greatest(count(), 1), 1) as hyde_pct,
            round(avg(exact_removed + semantic_removed), 1) as avg_dedup_removed,
            round(avg(latency_ms), 1) as avg_latency
        FROM rag_retrieval
        WHERE timestamp > now() - INTERVAL {0} DAY
