"""LLM judge through LiteLLM (BENCH_LITELLM_KEY: judge + tier 5 allowlist, docs/CONTRACTS.md §8)."""

import json
import re
import time

import httpx

from bench.checks import JudgeError

SYSTEM = (
    "Você é um avaliador rigoroso de respostas de um agente de IA. Avalie a RESPOSTA para a TAREFA usando "
    "apenas a RUBRICA. Dê uma nota de 0 a 10 (10 = cumpre todos os critérios, 0 = não cumpre nenhum). "
    "Penalize afirmações inventadas, formato errado e critérios ausentes. Responda SOMENTE com JSON no formato "
    '{"score": <número de 0 a 10>, "justificativa": "<até 3 frases>"}.'
)
_SCORE = re.compile(r'"score"\s*:\s*(-?\d+(?:\.\d+)?)')


class LiteLLMJudge:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120,
                 client: httpx.Client | None = None):
        if not api_key:
            raise ValueError("BENCH_LITELLM_KEY is required for llm_judge checks")
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.model = model
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.http = client or httpx.Client(timeout=timeout)

    def score(self, *, prompt: str, rubric: str, answer: str) -> tuple[float, str]:
        body = {
            "model": self.model, "temperature": 0, "max_tokens": 400,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"TAREFA:\n{prompt}\n\nRUBRICA:\n{rubric}\n\nRESPOSTA:\n{answer[:20000]}"},
            ],
        }
        last: Exception | None = None
        for attempt in range(3):
            if attempt:
                time.sleep(2 * attempt)
            try:
                resp = self.http.post(self.url, json=body, headers=self.headers)
            except httpx.HTTPError as exc:
                last = exc
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = JudgeError(f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                raise JudgeError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            try:
                content = resp.json()["choices"][0]["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise JudgeError(f"malformed completion: {exc}") from None
            return parse_verdict(content)
        raise JudgeError(f"judge unavailable after retries: {last}")


def parse_verdict(content: str) -> tuple[float, str]:
    """Score and reason from the judge's reply (tolerates prose or a fenced block around the JSON)."""
    reason = ""
    score = None
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            score = float(data.get("score"))
            reason = str(data.get("justificativa") or data.get("reason") or "")
        except (ValueError, TypeError, AttributeError):
            score = None
    if score is None and (sm := _SCORE.search(content)):
        score = float(sm.group(1))
    if score is None or not 0 <= score <= 10:
        raise JudgeError(f"unparseable judge verdict: {content[:200]!r}")
    return score, reason or content[:500]
