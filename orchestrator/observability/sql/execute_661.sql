SELECT
                        model,
                        COUNT(*) as queries,
                        AVG(model_load_latency_ms) as avg_load_ms,
                        AVG(prompt_eval_latency_ms) as avg_prompt_eval_ms,
                        AVG(generation_latency_ms) as avg_gen_ms,
                        AVG(first_token_latency_ms) as avg_first_token_ms,
                        AVG(generation_tokens_per_second) as avg_gen_tps,
                        AVG(prompt_tokens_per_second) as avg_prompt_tps,
                        SUM(CASE WHEN cold_start = 1 THEN 1 ELSE 0 END) as cold_starts
                    FROM llm_call_log
                    WHERE timestamp >= ?
                    GROUP BY model ORDER BY queries DESC
