SELECT count() as cnt FROM llm_events WHERE event IN ('request_completed', 'llm_call_completed')
