# Audio Transcribe Agent

Servico HTTP assíncrono para transcricao de audio/video. O contrato duravel
vive em `SPEC.md`; este README resume a superficie operacional.

`audio_transcribe` e dono de STT batch, upload, streaming, VAD, segmentos,
exports e contratos de transcricao. Nao e dono de microfone, PipeWire,
playback, TTS, wake word, turn-taking ou decisao agentic.

## Contrato API

Base interna batch: `https://audio-transcribe:8080`

Base interna streaming: `https://audio-streaming:8087`

Autenticacao: `X-API-Key: <audio-token>` ou
`Authorization: Bearer <audio-token>`. A auth e fail-closed por defeito; dev
sem auth exige `AUDIO_TRANSCRIBE_SECURITY_ALLOW_UNAUTHENTICATED_DEV=true`.

| Metodo | Path | Uso |
| --- | --- | --- |
| GET | `/health` | Estado GPU/modelo/fila/recovery |
| POST | `/v1/transcribe` | Endpoint canonico de dispatch para `AudioQueryRequest -> AudioQueryResponse` |
| POST | `/transcriptions` | Criar job a partir de `input_path` visivel no container |
| POST | `/transcriptions/upload` | Criar job por upload multipart com leitura chunked e quota |
| GET | `/transcriptions/{job_id}` | Estado/progresso |
| GET | `/transcriptions/{job_id}/result` | Resultado final |
| GET | `/transcriptions` | Listar jobs |
| POST | `/transcriptions/{job_id}/cancel` | Cancelar job |
| DELETE | `/transcriptions/{job_id}` | Remover job/outputs |
| POST | `/cleanup?dry_run=true\|false` | Cleanup protegido de jobs terminais expirados |
| GET | `/models` | Modelos suportados |
| GET | `/config` | Config sanitizada |
| GET | `/metrics` | Metricas do servico/fila/recovery |
| WS | `/ws/stream` | Streaming realtime PCM16 no sub-servico `audio-streaming` |
| GET | `/stream/{job_id}` | Stream do sub-servico realtime |
| GET | `/stream/{job_id}/segments` | Segmentos do stream |

Exemplo batch:

```bash
curl -sS https://audio-transcribe:8080/transcriptions \
  -H "X-API-Key: $AUDIO_TRANSCRIBE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input_path":"/data/input/reuniao.wav","options":{"language":"pt","model":"distil-large-v3","rag_ready":true}}'
```

Resposta inicial:

```json
{"job_id": "abc123", "status": "queued", "created_at": "2026-06-06T10:00:00Z", "status_url": "/transcriptions/abc123"}
```

## Integracao

- URLs centrais: `[services].audio_transcribe_url` e `[services].audio_streaming_url`.
- O gateway audio usa a config central e o manifest `agents/service_capabilities.toml`.
- `POST /v1/transcribe` e o path canonico publicado no manifest e em `config/orc/agents.toml`.
- Ffmpeg/fingerprint sao ferramentas locais permitidas, nao canais de comunicacao entre containers.
- No runtime Docker, `${AUDIO_TRANSCRIBE_DATA_DIR}/input` e montado read-only; o agente escreve outputs em scratch validado e o `storage_guardian` projeta a vista persistente em `${AUDIO_TRANSCRIBE_DATA_DIR}/output/<job_id>/`.
- Publicacao duravel e projecao gerida de artefactos sao responsabilidade do `storage_guardian`; o runtime usa politica obrigatoria e o modo optional fica reservado a desenvolvimento/testes isolados.
