SELECT intent, count() as cnt
                FROM llm_events
                WHERE timestamp >= '{0}' AND event IN ('request_completed', 'llm_call_completed')
                  AND model = '{1}'
                GROUP BY intent
