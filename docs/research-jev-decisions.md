# Decision Service backends: "Jev" (TypeSafe AI) + "OpenAI Decisions" + local rules/LLM stand-in
RAM: Go Decision Service ~30-60 MB. qwen3:4b Q4_K_M in Ollama ~2.6 GB of weights, roughly 3,000-4,000 MB resident at 4-8k context (shared with the rest of the stack; unloads after OLLAMA_KEEP_ALIVE). Jev / OpenAI Decisions backends add 0 MB locally (remote APIs).

IDENTIFICATION (high confidence for both)

1) "Jev" is TypeSafe AI's "System One" decision model. It is not a chat LLM. You send it application `state` plus named, typed questions, and it returns typed answers with probabilities and a confidence value in one parallel pass. It produces no text, no reasoning and no explanations. According to secondary press (YourStory, The Neuron, Eden AI), it came out of stealth around 2026-09-15 with a $40M seed round led by DCVC, and the founder is Diogo Almeida, an ex-OpenAI researcher and RLHF/InstructGPT co-author. I did not confirm those funding and founder details on typesafe.ai itself. The rest comes from the official docs at docs.typesafe.ai:
- Endpoint: `POST https://api.typesafe.ai/v1/systemone`, auth with `Authorization: Bearer $TYPESAFE_API_KEY`.
- Request: `{state: string|object|array, model: "jev-latest", questions: {<name>: Question}}`.
- Three question types:
  - `noul` (yes/no): returns a probability in 0-1. Optional `criteria` takes `{true, false}` descriptions.
  - `choice`: `criteria` is a map of option to description, up to 255 options. Returns `choice`, `probabilities` and `confidence`.
  - `score`: `criteria` is an ordered array of 2-10 levels. Returns `score`, `legend`, `probabilities` and `confidence`.
- You can mix question types in one call, and they are evaluated in parallel.
- Models: `jev-1.13.0` is current. The aliases `jev-latest` and `jev-preview` both point to it.
- Context: 64k tokens per request, with 32k for state plus the longest question. Text only. English is best; other languages work with lower accuracy.
- Price: $0.042 per 1M input tokens. Output is free.
- Rate limits: 100K tokens/s and 80 req/s, described as "adjusting dynamically".
- Errors: 401, 422, 429 and 529. Retry 429/529 with backoff.
- SDKs: Python `pip install typesafe-sdk` (Python 3.10+, `from typesafe_sdk import TypeSafeClient, Noul`, `client.system_one(...)`) and JS `npm i @typesafe-ai/sdk` (v0.6.0, Node 20+, `client.systemOne(...)`). There is also a Claude Code plugin: `claude plugin marketplace add typesafe-ai/skills`.
- It is also on OpenRouter as `typesafe/jev-1.13` and `~typesafe/jev-latest`. OpenRouter's page lists a 32k context, and calls go through `POST https://openrouter.ai/api/alpha/decisions` (or `/api/v1/systemone`), not chat/completions.
- It is not open-weights or self-hostable. Calibrated confidence is the selling point: you set a threshold for acting automatically versus escalating to a human.

2) "OpenAI Decisions" is the OpenAI Decisions API, announced at DevDay on 2026-09-29. According to secondary sources it went to public beta on 2026-10-06, with GA "in the coming weeks". The official guide is developers.openai.com/api/docs/guides/decisions:
- Endpoint: `POST https://api.openai.com/v1/decisions`. Python: `client.decisions.create(model="gpt-6-luna", input=..., questions=[...])`. The only model is `gpt-6-luna`, a specialised variant.
- `input` can be a string or user messages with `input_text` and `input_image`. Images must be base64 data URLs. Each question carries `type` and `name`.
- Question types:
  - `predicate`: returns `probability`.
  - `choice`: returns `choice`, `probabilities` and `confidence`.
  - `score`: returns `score` and `confidence`.
- An answer can come back with `type: "refusal"`. `answers` is an array keyed by `name`.
- Price: $0.10 per 1M input tokens. There are no output or cache charges, but regional and long-context multipliers apply.
- Speed: OpenAI says it is "10x faster than Responses", and press reports about 150 ms.
- For independent questions, put them in one call. If one decision depends on another, make separate requests.
- The docs do not mention LiteLLM support.

The two APIs are almost the same shape: Jev's noul matches OpenAI's predicate, and choice and score match directly. The differences are that Jev keys questions in a map while OpenAI uses an array with `name`, and Jev takes `state` while OpenAI takes `input`. So one internal schema can cover both.

IMPORTANT: neither API is a chat-completions model, so LiteLLM cannot route to them like other models. The Decision Service should call them directly over HTTP, or through LiteLLM pass-through endpoints, which I have not verified for these APIs. Log the calls to Langfuse manually.

LOCAL STAND-IN (cheap, runs on this laptop)

Use one internal schema that copies the shared Jev/OpenAI shape. Then each backend is only a mapper, and wiring up Jev later means writing one adapter.
- Request: `{state any; questions []Question{Name, Type: binary|choice|score, Instructions, Criteria map[string]string | Levels []string}; threshold float}`.
- Answer: `{Name, Type, Choice, Probability, Score, Probabilities map[string]float64, Confidence, Backend, Refused}`.

Use a cascade of backends, cheapest first:
- (a) RulesEngine: deterministic Go rules using expr-lang/expr or CEL over `state`. Examples are regex/keyword routing and hard policies. A rule that matches returns confidence 1.0. Hard permissions always stay in code.
- (b) LocalLLMEngine: Qwen3 4B through LiteLLM (`ollama_chat/qwen3:4b`) with `temperature: 0`, thinking off, and `response_format` set to a json_schema per question:
  - choice: `{"type":"object","properties":{"choice":{"type":"string","enum":[...options]}},"required":["choice"]}`
  - binary: `enum: ["yes","no"]`
  - score: an enum of the level indices.
  Ollama enforces the schema as a grammar, so the output always parses. For probabilities and confidence there are two options:
  - Call Ollama's native `/api/chat` with `logprobs: true, top_logprobs: 5, think: false, format: <schema>`. Read the token distribution where the enum value starts and renormalise it over the options. The docs confirm these `/api/chat` fields; I have not checked whether LiteLLM passes logprobs through for Ollama.
  - Self-consistency: run k=3-5 samples at temp 0.7 and use the vote share as the probability. This matches TypeSafe's own "self-consistency" cookbook pattern.
  Confidence = 1 minus the normalised entropy of the probabilities, or the top-1 minus top-2 margin.
- (c) Escalation: if confidence is below the threshold, send it to JevEngine. Either do this now (it costs a fraction of a cent per decision, at $0.042/M) or later. Use OpenAIDecisionsEngine for image inputs. If nothing is configured, mark it as `needs_human`.

Log every decision (request hash, backend, probabilities, latency) to Postgres and Langfuse. That gives you a golden set to measure local-versus-Jev agreement before switching over.

Wire backends from environment variables so the stack moves to Railway unchanged: `DECISION_BACKENDS=rules,local,jev`, `DECISION_THRESHOLD=0.8`, `LITELLM_BASE_URL=http://litellm:4000`, `TYPESAFE_API_KEY`, `OPENAI_API_KEY`.

Qwen3 4B Q4_K_M should give about 0.3-1.5 s per short decision on a Ryzen 7 250 CPU or the 780M through Vulkan. That is my estimate and I have not benchmarked it. Jev is about 70-500 ms.

## CMDS
Go interface:
```go
type QType string // "binary" | "choice" | "score"
type Question struct { Name, Instructions string; Type QType; Criteria map[string]string; Levels []string }
type DecisionRequest struct { State any; Questions []Question; Threshold float64; Images []string }
type Answer struct { Name string; Type QType; Choice string; Probability, Score, Confidence float64; Probabilities map[string]float64; Backend string; Refused, NeedsHuman bool }
type DecisionEngine interface { Decide(ctx context.Context, r DecisionRequest) ([]Answer, error) }
// impls: RulesEngine, LocalLLMEngine, JevEngine, OpenAIDecisionsEngine, CascadeEngine
```
Jev call (verified from docs):
```
curl -X POST https://api.typesafe.ai/v1/systemone -H "Authorization: Bearer $TYPESAFE_API_KEY" -H "Content-Type: application/json" \
 -d '{"state":"My shoes arrived in the wrong size","model":"jev-latest","questions":{"department":{"type":"choice","instructions":"Which team should handle this?","criteria":{"returns":"Exchanges, wrong or damaged items","shipping":"Delivery status","billing":"Charges, invoices"}}}}'
# -> {"model":"jev-1.13.0","answers":{"department":{"type":"choice","choice":"returns","confidence":1.0,"probabilities":{...}}},"usage":{...}}
```
OpenAI Decisions (beta):
```
curl https://api.openai.com/v1/decisions -H "Authorization: Bearer $OPENAI_API_KEY" -H "Content-Type: application/json" \
 -d '{"model":"gpt-6-luna","input":"...","questions":[{"type":"predicate","name":"urgent","instructions":"Is this urgent?"}]}'
# -> {"answers":[{"type":"predicate","name":"urgent","probability":0.92}]}
```
LiteLLM config.yaml (local decider):
```yaml
model_list:
  - model_name: decider-local
    litellm_params:
      model: ollama_chat/qwen3:4b
      api_base: http://host.docker.internal:11434
      temperature: 0
```
docker-compose (litellm + decision service): `extra_hosts: ["host.docker.internal:host-gateway"]`; decision env: `DECISION_BACKENDS=rules,local,jev`, `DECISION_THRESHOLD=0.8`, `LITELLM_BASE_URL=http://litellm:4000`, `OLLAMA_BASE_URL=http://host.docker.internal:11434`, `TYPESAFE_API_KEY=`, `OPENAI_API_KEY=`.
Native Ollama call with logprobs (for confidence):
```
POST http://host.docker.internal:11434/api/chat
{"model":"qwen3:4b","think":false,"stream":false,"logprobs":true,"top_logprobs":5,"options":{"temperature":0},
 "format":{"type":"object","properties":{"choice":{"type":"string","enum":["returns","shipping","billing"]}},"required":["choice"]},
 "messages":[{"role":"system","content":"Classify. Answer only with JSON."},{"role":"user","content":"<state + question + criteria>"}]}
```
Optional SDKs: `pip install typesafe-sdk`; `npm install @typesafe-ai/sdk`; `pip install -U openai` (needs a version with client.decisions).

## RISKS
- Jev's funding ($40M seed, DCVC), founder (Diogo Almeida) and 2026-09-15 launch date come only from secondary press; typesafe.ai and docs.typesafe.ai confirm the product, API, pricing and models but I did not see those company details there.
- Jev context limits conflict: TypeSafe docs say 64k per request (32k for state + longest question), while OpenRouter's guide says 32k total.
- TypeSafe rate limits (80 req/s, 100K tok/s) are documented as 'adjusting dynamically'.
- OpenAI Decisions API is in beta (public beta around 2026-10-06 per secondary sources; launched 2026-09-29). The schema and pricing ($0.10/M input, plus regional and long-context multipliers) could change before GA. Max questions and options are not documented. The openai.com DevDay recap returned 403, so I could not read it.
- Neither Jev nor OpenAI Decisions is a chat/completions model, so LiteLLM cannot route them as normal models. Whether LiteLLM pass-through endpoints work for them is unverified; plan for direct HTTP from the Decision Service.
- I did not confirm whether LiteLLM forwards logprobs/top_logprobs for ollama_chat. The Ollama native /api/chat docs do list logprobs, top_logprobs and think fields, so the confidence path should call Ollama natively, or fall back to self-consistency voting.
- I did not check which Qwen3 4B revision the Ollama tag 'qwen3:4b' currently points to (original hybrid-thinking or 2507 instruct), so thinking must be disabled explicitly with think:false.
- Local latency and accuracy for Qwen3 4B on a Ryzen 7 250 / 780M have not been benchmarked. The 0.3-1.5 s figure is my estimate.
- The OpenRouter 'api/alpha/decisions' endpoint is labelled alpha.

## SOURCES
https://typesafe.ai
https://docs.typesafe.ai/llms.txt
https://docs.typesafe.ai/introduction/quickstart
https://docs.typesafe.ai/api.md
https://docs.typesafe.ai/models.md
https://docs.typesafe.ai/primitives/choice.md
https://docs.typesafe.ai/sdk/javascript.md
https://openrouter.ai/docs/guides/community/jev
https://openrouter.ai/models/typesafe/jev-1.13
https://yourstory.com/ai-story/what-is-jev-typesafe-ai-decision-model
https://www.theneuron.ai/explainer-articles/typesafe-jev-system-one-models-explained/
https://developers.openai.com/api/docs/guides/decisions
https://rits.shanghai.nyu.edu/ai/openais-decisions-api-picks-answers-instead-of-writing-them/
https://www.orcarouter.ai/blog/openai-decisions-api-public-beta
https://openai.com/index/devday-2026-recap/ (403, not readable)
https://docs.litellm.ai/docs/providers/ollama
https://docs.ollama.com/capabilities/structured-outputs
https://docs.ollama.com/api/chat