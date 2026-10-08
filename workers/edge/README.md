# edge — agent-edge media worker

Implements docs/ARCHITECTURE.md §22–23 and CONTRACTS.md §5. It is a FastAPI service (`http://edge:8080`, host `127.0.0.1:8093`, compose profile `media`) that turns audio and video into transcripts, summaries and knowledge documents:

```
upload → storage://media/input/<uuid>.<ext>
       → ffmpeg (16 kHz mono WAV, media/processing/)          → whisper.cpp whisper-server (/inference, verbose_json)
       → media/transcripts/<id>.{txt,srt,json}                 → LiteLLM local-qwen map-reduce (pt-BR JSON)
       → media/transcripts/<id>.summary.{md,json}              → knowledge POST /v1/ingest (2 documents)
       → original moved to media/archive/ (never overwrites; deleted after MEDIA_RETENTION_DAYS, swept automatically)
```

It is not an agent: no loop, no decisions. Hermes (or `make`) calls it like any other tool backend.

## API

Every route except `GET /healthz` and `GET /readyz` needs `Authorization: Bearer <key>`, compared in constant time against every configured key:

- `EDGE_API_KEY` is the **admin** key (operator, `make` targets, the host). It reaches every route and tenant.
- `EDGE_TENANT_KEYS` (`nitro:<key>,pessoal:<key>`) are **tenant keys** for agent sessions. A tenant key can only call `POST /upload`, `POST /pipeline` and `GET /jobs[/{id}]` (anything else is 403 before a handler runs), and:
  - `/pipeline` must name that tenant, and its source must be in `storage://media/input/` (not another tenant's archive or transcripts);
  - `GET /jobs` lists, and `GET /jobs/{id}` returns, only jobs that key created (another key's job id is a 404).

  This closes the hole the aios `pre_tool_call` tenant guard can't see: it only inspects knowledge tool arguments, not `curl` to edge from the sandbox terminal. See *Wiring* below for what Hermes must pass.

Errors come back as `{"error": "..."}` with a meaningful status: 400 for a bad URI or tenant, 403 for a tenant key outside its tenant/routes, 404 for a missing source, 413 for a file that is too big, 415 for a disallowed extension, 422 for media ffmpeg can't read, 429 for a full queue, 501 for diarization without the extra, 502/503/504 for whisper, LiteLLM or knowledge failures.

| Endpoint | Body | Result |
|---|---|---|
| `POST /upload` | multipart, field `file` | `{id, uri: storage://media/input/<uuid>.<ext>, filename, ext, kind: audio\|video, bytes, sha256}` (201) |
| `POST /extract_audio` | `{source}` | `{id, uri: storage://media/processing/<id>.wav, duration_s, media_duration_s, has_video, sample_rate, channels}` |
| `POST /transcribe` | `{source, language='pt', diarize=false, force=false}` | `{id, text, segments[{start,end,text,speaker?}], txt_uri, srt_uri, json_uri, duration_s, cached}` |
| `POST /summarize` | `{text \| transcript_uri, kind: summary\|notes\|tasks\|topics\|all (default all), model='local-qwen'}` | `{summary, notes[], tasks[{title, due?, owner?}], topics[], meta{model, models_used, chunks, llm_calls, fallback_used, …}}` |
| `POST /process_video` | `{source, language, kind, model, diarize, force, title?}` | `{id, transcript{…}, summary{…}, summary_uri}` |
| `POST /pipeline` | `{source, tenant, domain?, ingest=true, language, model, title?, diarize, force}` | `{id, txt_uri, srt_uri, summary_uri, summary, ingest[{document, action, source_uri}], archived_uri, warnings[]}` |
| `POST /embed` | `{texts[]}` (1–256, ≤32k chars each) | `{model, dim, vectors[][]}` (LiteLLM `embed-local`) |
| `GET /jobs/{id}`, `GET /jobs?limit=&tenant=` | — | job `{job_id, kind, owner, tenant, status: queued\|running\|succeeded\|failed\|cancelled, result?, error?}`; the list never includes results |
| `POST /maintain?dry_run=` | — | retention report (same as `edge maintain`) |

Notes:

- **Sources** are `storage://media/...` URIs. Resolution is copied from `workers/kb/src/kb/storage.py`. `..`, absolute keys, backslashes, NUL bytes and symlinks that escape `STORAGE_LOCAL_ROOT` are rejected. Other buckets get 400.
- **Long jobs.** Add `"async": true` to any of extract/transcribe/summarize/process_video/pipeline. The response is `202 {job_id, status_url}`; poll `GET /jobs/{id}`. Synchronous calls go through the same queue.
  - The queue runs `EDGE_JOB_WORKERS=1` job at a time (RAM) and holds `EDGE_QUEUE_MAX` pending jobs.
  - The store keeps the last `EDGE_JOBS_MAX` jobs, in memory only. A restart forgets them, but files already written stay.
  - Cheap validation (URI, tenant, domain, diarization) fails immediately, before queueing.
  - Run a single uvicorn process (no `--workers`).
- **Ids.** An uploaded file keeps its uuid. A WAV that `/extract_audio` wrote (`processing/<id>.wav`) keeps `<id>`. Anything else gets `<slug>-<hash>`, where the 12-hex hash covers the URI **and that version of the file**: size, mtime and its first/last 64 KB.
  - So a second recording dropped at the same path (`input/reuniao.mp4`, phone exports, `aula05.m4a` again) gets a new id: its transcript, summary, KB documents (`source_uri` = `transcripts/<id>.*`) and archive copy never replace the first one's.
  - Transcripts are reused when `<id>.json` matches the source (URI, size, mtime), language and diarize. `force: true` re-transcribes.
  - Because of this reuse, a pipeline that failed at ingest doesn't pay for whisper again.
- **Scratch files.** A transcription extracts into its own `processing/<id>.<job>.wav` (+ `.chunks/`) and deletes them at the end. It never touches the `processing/<id>.wav` that `/extract_audio` handed to a caller, and two jobs on the same source (`EDGE_JOB_WORKERS>1`) don't share files. Atomic writes use `<name>.<random>.part` for the same reason.
- **Pipeline order.** transcribe → summarize (`all`) → store the summary → ingest (transcript, then summary) → archive.
  - The original moves from `input/` to `archive/` only after everything succeeded. The move never replaces a file: a taken name becomes `archive/<name>-1.<ext>`, `-2`, … (hard link + unlink, so it's atomic). The name is reserved before ingest, so the KB metadata `media_uri` points at the real file.
  - A transcript with no speech is not ingested; you get a warning instead.
  - `source_type` is `audio` or `video`, from the extension. `tenant` and `domain` are passed through, and the tenant is checked against `KNOWLEDGE_TENANTS` before any work starts.
- **Upload** is parsed incrementally (python-multipart) and written straight to `<name>.part`, so 2 GB never sits in RAM or in a temp spool.
  - The limit (`EDGE_MAX_UPLOAD_MB`) is checked against `Content-Length` and again while streaming.
  - Part headers are bounded by the parser itself, before any callback sees them: at most 8 headers per part and 4096 bytes per header line (python-multipart ≥ 0.0.31, passed explicitly). Non-file fields are capped at 64 KB in total.
  - Allowed extensions — audio: aac aif aiff amr caf flac m4a mp3 oga ogg opus wav weba wma. Video: 3g2 3gp avi flv m2ts m4v mkv mov mp4 mpeg mpg mts ogv ts webm wmv.

## Transcription (whisper.cpp `whisper-server`)

This contract was verified against `examples/server/server.cpp` at v1.9.5 (2026-10-06) and master, and by running a CPU build of v1.9.5 against `tests/test_integration.py`.

- `POST /inference` is multipart with the audio in `file`. We send `response_format=verbose_json`, `language=<lang>`, `token_timestamps=false` and `no_language_probabilities=true`.
- verbose_json returns `{task, language, duration, text, segments[{id, text, start, end, tokens, words, temperature, avg_logprob, no_speech_prob, speaker?}]}`, with `start`/`end` in seconds.
  - verbose_json switches token timestamps on by default, and that wraps segments at 60 characters mid-sentence. Sending `token_timestamps=false` keeps whisper's natural segments.
  - `no_language_probabilities` skips an extra language-detection pass.
- Audio longer than `EDGE_WHISPER_CHUNK_SECONDS` (600) is cut into WAV chunks. Each cut lands on the quietest 100 ms of the 10 s before the boundary, so words aren't split. Segments are shifted back by each chunk's offset.
  - This bounds whisper-server's RAM, because it buffers the upload plus float32 PCM.
  - A tail shorter than 30 s is merged into the last chunk.
- The `.txt` output has one segment per line, like `whisper-cli -otxt`. The `.srt` output is standard SRT. Speaker labels appear as `[SPEAKER_00]` when diarized.

**The `whisper` service in compose.yaml** (applied). verbose_json itself needs **no** flag. But with `user: 1000:1000` and `--convert`, whisper-server writes temp files to `--tmp-dir`, which defaults to `.`, and in the image `.` is `/app`, owned by root. So:

```yaml
  whisper:
    restart: unless-stopped
    command: ["whisper-server", "--host", "0.0.0.0", "--port", "8080", "-m", "/models/ggml-large-v3-turbo-q5_0.bin",
              "-l", "pt", "--convert", "--tmp-dir", "/tmp"]
    healthcheck: # the image ships curl; /health returns 503 while the model loads
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]
      interval: 30s
      start_period: 60s
```

Edge always sends 16 kHz mono WAV, so `--convert` is only for other callers. Flash attention is already on by default in v1.9.5. With the whisper healthcheck in place, edge waits on `whisper: {condition: service_healthy}`.

## Summaries (map-reduce for a 4K-context local model)

- **Chunking.** Text is split by paragraph, then line, then sentence, then word into chunks of `EDGE_MAP_CHUNK_TOKENS` (2500). Tokens are estimated at 3.2 chars/token, which is conservative for pt-BR.
- **Output budget.** Every prompt states explicit caps, sized so that a maximal answer fits that call's `max_tokens` (a test builds the worst case and checks it):

  | | max_tokens | summary | notes | tasks | topics |
  |---|---|---|---|---|---|
  | local map / reduce | 800 / 1100 | ≤60 / ≤120 words | 6 / 8 | 6 / 8 | 5 / 8 |
  | other models map / reduce | 3000 / 4000 | ≤250 / ≤300 words | 15 | 20 | 8 |

  Notes have ≤20 words, task titles ≤12. For `local-qwen` (4096-token context): chunk + 600 tokens reserved for system prompt, instructions, format spec and a retry note + 800 = 3900. A reduce gets 2200 tokens of partials. Keep `EDGE_MAP_CHUNK_TOKENS` + 1400 within the local model's context.
- **Map, then reduce.** Each chunk produces a partial `{summary, notes, tasks, topics}`. Partials are reduced in batches that fit the reduce budget, hierarchically, until one remains. A batch closes only once it has two partials (so reduction converges). A trailing single partial is carried to the next round without a call, instead of being forced into a batch it doesn't fit. A single chunk is a single call.
- **Strict JSON.** Requests use `response_format: json_object` and a pt-BR system prompt. The parser tolerates `<think>`, fences, prose around the object, trailing commas, comments, smart quotes and Python literals. Answers are normalized (pt keys such as `resumo`/`tarefas` are accepted, lists deduplicated and capped at the call's limits).
- **Cut answers are never accepted.** `finish_reason: length` means the last fields (usually `topics`, or a task cut mid-title) are missing. The call is repeated from the same input with half the caps; if that is cut too, the call goes to the fallback (bigger budget), else it fails with 502.
- **Retries fit the same context.** A retry resends the original prompt with a short note appended to the user turn (no bad reply echoed back, no extra turns). The 600-token reserve covers that note.
- **Fallback.**
  - A call whose answer has no usable JSON is retried once; if it still fails, the call goes to `EDGE_FALLBACK_MODEL` (`tier2-cheap`).
  - A prompt that would not fit the local context (e.g. two oversized partials) goes straight to the fallback.
  - A transport or HTTP failure of the primary (down, timeout, context exceeded) sends the rest of that job to the fallback.
  - Text estimated above `EDGE_LOCAL_MAX_TOKENS` (24000) skips the local model and uses the fallback with `EDGE_FALLBACK_CHUNK_TOKENS` (24000) chunks.
  - `meta.models_used` and `meta.fallback_used` report what happened.

## Retention (§23)

Edge runs retention by itself: once at startup and then every `EDGE_MAINTAIN_INTERVAL_HOURS` (24; `0` turns the schedule off), in a background task of the server process. Each sweep logs its JSON report (`"msg": "retention done"`). A failed sweep is logged and the schedule continues.

`edge maintain [--dry-run] [--retention-days N] [--stale-hours H]` runs it by hand. It is the same as `POST /maintain` (admin key) and `make edge-maintain`, and prints the JSON report.

- `media/archive/` files older than `MEDIA_RETENTION_DAYS` (30) are deleted. `0` keeps them forever. Archiving resets the mtime, so the clock starts at archive time.
- `media/processing/` files older than `EDGE_PROCESSING_STALE_HOURS` (24) are deleted, and empty dirs removed. Jobs already delete their own WAVs and chunks. A WAV you created with `/extract_audio` stays until the next sweep. Keep this above the longest transcription job.
- Leftover `*.part` files in `input/` and `transcripts/` (aborted uploads, interrupted writes) go too.
- `media/input/` (not processed yet) and `media/transcripts/` are never deleted.
- Symlinks are never followed.


## Diarization (optional)

`diarize: true` returns **501** with instructions unless the image was built with `--build-arg EDGE_EXTRAS=diarize` **and** `HF_TOKEN` is set. The token's account must have accepted the terms of `EDGE_DIARIZE_MODEL`, which defaults to `pyannote/speaker-diarization-community-1`. Each whisper segment gets the speaker with the largest time overlap. pyannote telemetry is off (`PYANNOTE_METRICS_ENABLED=0`).

- **CPU-only build.** The extra pins `torch`, `torchaudio` and `torchcodec` to `https://download.pytorch.org/whl/cpu` (`[tool.uv.sources]`, explicit index). PyPI's Linux torch drags in ~15 `nvidia-*` CUDA packages, and PyPI's torchcodec won't even load without `libnvrtc`. Locked: torch 2.14.1+cpu, torchaudio 2.11.0+cpu, torchcodec 0.17.0+cpu, pyannote.audio 4.0.7. The extra downloads about 340 MB and adds about 1.3 GB to the venv (measured with `uv sync --extra diarize`). pyannote decodes the 16 kHz WAV through torchcodec and the image's FFmpeg libraries.
- **Model cache.** The runtime user has no home directory, so the image sets `HOME=/tmp` and `HF_HOME=/data/hf-cache`. compose.yaml mounts the named volume `edge_hf_cache` there, so the model is downloaded once, not after every restart. A model that can't be loaded (bad token, terms not accepted, offline hub) is a 503 with a hint, not a 500.
- The 768 MB `mem_limit` is too small for pyannote: raise it (about 3 GB) if you enable the extra.

## Container hardening (`compose.yaml` service `edge`)

ffmpeg parses untrusted media (forwarded WhatsApp audio, downloaded videos). Its parsers have a long CVE history, so the container keeps the blast radius small:

- `cap_drop: [ALL]`, because uid 1000 on port 8080 needs no capabilities; `security_opt: no-new-privileges`.
- `read_only: true`. The code and venv are baked in; writes go only to `./data/storage`, the `edge_hf_cache` volume and a 64 MB `/tmp` tmpfs (`HOME`).
- `pids_limit: 256`. Threads count as pids: anyio's 40-thread pool, the job workers, ffmpeg threads, and torch/OpenMP when diarizing.
- `stop_grace_period: 30s`, which is longer than uvicorn's 20 s graceful shutdown. A job still running after that is abandoned by SIGKILL. That is by design: jobs live in memory, outputs are written atomically, and the job's `processing/` leftovers are swept by retention.

## Run / test

```bash
make edge-up                                   # docker compose --profile media up -d --build --wait edge (and whisper)
make edge-pipeline f=~/aula.m4a tenant=pessoal domain=learning title="Aula 3"   # upload + async pipeline
make edge-job id=<job_id>                      # poll
make edge-maintain dry=1
make edge-test                                 # = uv run --project workers/edge pytest -q workers/edge/tests (also part of make test)
make edge-dev                                  # run on the host at :8093 (needs ffmpeg on the host)
```

The tests are offline. ffmpeg is replaced by a fake `subprocess.run` that writes real WAVs, and whisper, LiteLLM and knowledge are mocked with respx.

- `test_ffmpeg_real.py` runs only when `ffmpeg`/`ffprobe` exist.
- `test_integration.py` talks to live services only when they answer:
  - `EDGE_IT_LLM_URL`, default `http://127.0.0.1:14100/v1`;
  - `EDGE_IT_KNOWLEDGE_URL`/`_KEY`, default `:18092`/`kk`. This test writes two documents with a fixed id into tenant `pessoal`;
  - `EDGE_IT_WHISPER_URL`, default `:8178`, plus an optional `EDGE_IT_WHISPER_SAMPLE` speech WAV.
- Set `EDGE_SKIP_INTEGRATION=1` to skip all of them.

## ENV

| Name | Secret | Default | Notes |
|---|---|---|---|
| `EDGE_API_KEY` | **yes** | — (required) | admin bearer for every route except health; generate like the other service keys (`hex 32`). Never give it to agent sessions |
| `EDGE_TENANT_KEYS` | **yes** | empty | `tenant:key,tenant:key` (each key ≥16 chars, unique, ≠ admin key; several keys per tenant allowed for rotation). A tenant key only reaches `/upload`, `/pipeline` for its tenant and its own `/jobs` |
| `EDGE_LITELLM_KEY` | **yes** | — | LiteLLM virtual key: `local-qwen`, `embed-local`, `tier2-cheap` (+ `tier2-flash` if callers pass it) |
| `KNOWLEDGE_API_KEY` | **yes** (existing) | — | for `/pipeline` ingest |
| `HF_TOKEN` | **yes** | empty | only with the `diarize` extra |
| `STORAGE_BACKEND` | no (existing) | `local` | edge supports only `local` (ffmpeg needs files) |
| `STORAGE_LOCAL_ROOT` | no (existing) | `/data/storage` | |
| `EDGE_MAX_UPLOAD_MB` | no | `2048` | |
| `WHISPER_URL` | no | `http://whisper:8080` | |
| `EDGE_WHISPER_CHUNK_SECONDS` | no | `600` | `0` = send the whole file |
| `EDGE_WHISPER_TIMEOUT` | no | `1800` | seconds per chunk request |
| `EDGE_FFMPEG_TIMEOUT` | no | `7200` | seconds |
| `LITELLM_BASE_URL` | no (existing) | `http://litellm:4000` | a trailing `/v1` is accepted |
| `EDGE_SUMMARY_MODEL` | no | `local-qwen` | default `model` of `/summarize` |
| `EDGE_FALLBACK_MODEL` | no | `tier2-cheap` | `none` disables the fallback |
| `EDGE_EMBED_MODEL` | no | `embed-local` | |
| `EDGE_MAP_CHUNK_TOKENS` | no | `2500` | map chunk for `local-*` models |
| `EDGE_FALLBACK_CHUNK_TOKENS` | no | `24000` | chunk for non-local models |
| `EDGE_LOCAL_MAX_TOKENS` | no | `24000` | above this, skip the local model |
| `EDGE_LLM_TIMEOUT` | no | `300` | seconds per LLM call |
| `KNOWLEDGE_URL` | no | `http://knowledge:8080` | |
| `EDGE_KNOWLEDGE_TIMEOUT` | no | `300` | ingest embeds synchronously |
| `KNOWLEDGE_TENANTS` | no (existing) | `nitro,pessoal,shared` | early tenant check for `/pipeline` |
| `MEDIA_RETENTION_DAYS` | no | `30` | `0` = keep the archive forever |
| `EDGE_PROCESSING_STALE_HOURS` | no | `24` | keep above the longest job |
| `EDGE_MAINTAIN_INTERVAL_HOURS` | no | `24` | retention at startup and then every N hours (fractions allowed); `0` = no schedule |
| `EDGE_JOB_WORKERS` | no | `1` | concurrent heavy jobs (RAM) |
| `EDGE_QUEUE_MAX` | no | `20` | queued + running jobs before 429 |
| `EDGE_JOBS_MAX` | no | `100` | jobs kept in memory |
| `EDGE_DIARIZE_MODEL` | no | `pyannote/speaker-diarization-community-1` | |
| `EDGE_LOG_LEVEL` | no | `INFO` | JSON logs to stdout |

Build arg: `EDGE_EXTRAS` (empty, or `diarize`). Set in the image, not meant to be overridden: `HOME=/tmp`, `HF_HOME=/data/hf-cache`.

**Wiring (done):**

- `.env.example` / `infra/scripts/gen-env.sh` generate `EDGE_API_KEY` (admin, host only) and
  `EDGE_TENANT_KEYS=nitro:<hex>,pessoal:<hex>,shared:<hex>`; `infra/scripts/litellm-keys.sh` mints `EDGE_LITELLM_KEY`
  (`local-qwen`, `embed-local`, `tier2-cheap`, `tier2-flash`).
- **Tenant isolation:** the hermes container gets only `EDGE_TENANT_KEYS`, never the admin key. The aios plugin sets
  `EDGE_API_KEY` in each Kanban worker to the key of the task's `HERMES_TENANT` (verified end to end with a
  gateway-dispatched worker), and the `transcript_to_notes` skill forwards it to the sandbox.
- `compose.yaml` has the `edge` service (profile `media`, on `aios` + `sandbox_net`), the `edge_hf_cache` volume and
  the whisper change above.
