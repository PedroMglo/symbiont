Analyze the following transcription and produce a structured summary in the same language as the text.

Transcription:
{text}

Produce a JSON response with this exact structure:
{{
  "summary": "2-3 sentence overview of the content",
  "key_points": ["point 1", "point 2", ...],
  "action_items": ["action 1", "action 2", ...],
  "decisions": ["decision 1", ...],
  "topics": ["topic 1", "topic 2", ...],
  "language": "detected language code (pt, en, es, etc.)"
}}

Rules:
- Use the SAME language as the transcription for all fields
- If no action items or decisions exist, use empty arrays
- Keep key_points concise (max 5-7 items)
- Be factual — only include what's actually in the text
