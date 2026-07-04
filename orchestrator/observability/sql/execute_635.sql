SELECT
                        COUNT(*) as total,
                        SUM(CASE WHEN cold_start = 1 THEN 1 ELSE 0 END) as cold_starts,
                        AVG(context_build_latency_ms) as avg_context_build_ms,
                        AVG(model_load_latency_ms) as avg_model_load_ms,
                        AVG(prompt_eval_latency_ms) as avg_prompt_eval_ms,
                        AVG(generation_latency_ms) as avg_generation_ms,
                        AVG(prompt_tokens_per_second) as avg_prompt_tps,
                        AVG(generation_tokens_per_second) as avg_gen_tps
                    FROM llm_call_log WHERE timestamp >= ?
