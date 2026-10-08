"""Map-reduce summaries in pt-BR sized for a 4K-context local model (Qwen3 4B via LiteLLM `local-qwen`).

map:    each chunk -> partial {summary, notes, tasks, topics}
reduce: partials (batched to fit the model's budget) -> one final object, repeated until one remains
Every call asks for strict JSON within explicit item caps (`Limits`) sized so a maximal answer fits its
max_tokens. An answer cut by max_tokens (finish_reason=length) is never accepted: the call is repeated from
the same input asking for fewer, shorter items. Unparseable JSON is retried once with a stricter note.
Retries resend the original prompt plus a short note, so they fit the context reserved for the first try.
The fallback model (EDGE_FALLBACK_MODEL) takes over a call the primary could not answer, a prompt that would
not fit the local context, the rest of the job after a transport/HTTP failure of the primary, and the whole
job when the text is too large for the local model (EDGE_LOCAL_MAX_TOKENS).
"""

import functools
import json
import logging
import math
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace

from edge.errors import UpstreamError
from edge.jsonx import JSONExtractionError, extract_json_object
from edge.llm import Completion, LLMClient, LLMError

log = logging.getLogger(__name__)

KINDS = ("summary", "notes", "tasks", "topics", "all")
FIELDS = ("summary", "notes", "tasks", "topics")
# Conservative for pt-BR with the Qwen tokenizer: overestimating tokens only makes chunks smaller.
CHARS_PER_TOKEN = 3.2
# Prompt tokens around the text/partials: system prompt, instructions, format spec and a retry note.
PROMPT_RESERVE = 600
MESSAGE_OVERHEAD = 8  # chat-template tokens per message
NOTE_WORDS, TASK_WORDS, TOPIC_WORDS = 20, 12, 4


@dataclass(frozen=True)
class Limits:
    """Output budget of one call; the caps are sized so a maximal answer fits max_tokens (see tests)."""

    max_tokens: int
    summary_words: int
    notes: int
    tasks: int
    topics: int

    def shorter(self) -> "Limits":
        return replace(
            self,
            summary_words=max(30, self.summary_words // 2),
            notes=max(2, self.notes // 2),
            tasks=max(2, self.tasks // 2),
            topics=max(3, self.topics // 2),
        )


# local-qwen: map prompt (chunk + PROMPT_RESERVE) + 800 stays under its 4096-token context.
LOCAL_LIMITS = {"map": Limits(800, 60, 6, 6, 5), "reduce": Limits(1100, 120, 8, 8, 8)}
REMOTE_LIMITS = {"map": Limits(3000, 250, 15, 20, 8), "reduce": Limits(4000, 300, 15, 20, 8)}

SYSTEM = (
    "Você extrai informações de transcrições e textos em português do Brasil. "
    "Responda SOMENTE com um objeto JSON válido: sem markdown, sem comentários, sem texto antes ou depois. "
    "Escreva todos os valores em português do Brasil. Não invente fatos, datas, prazos ou responsáveis "
    "que não estejam no texto."
)
FIELD_SPECS = {
    "summary": '"summary": string com um resumo fiel e objetivo, de no máximo {summary_words} palavras',
    "notes": (
        '"notes": lista com no máximo {notes} strings: pontos-chave, decisões e informações importantes '
        "(uma ideia por item, até {note_words} palavras cada)"
    ),
    "tasks": (
        '"tasks": lista com no máximo {tasks} objetos {{"title": string, "due": string ou null, '
        '"owner": string ou null}} com ações/tarefas mencionadas (title com até {task_words} palavras); '
        "due só se houver prazo no texto, owner só se houver responsável"
    ),
    "topics": '"topics": lista com no máximo {topics} temas principais, curtos (1 a {topic_words} palavras cada)',
}
EXAMPLE = {
    "summary": "...",
    "notes": ["..."],
    "tasks": [{"title": "...", "due": None, "owner": None}],
    "topics": ["..."],
}
RETRY_JSON = "ATENÇÃO: responda SOMENTE com o objeto JSON no formato pedido acima, começando com { e terminando com }."
RETRY_SHORTER = (
    "ATENÇÃO: uma resposta anterior ficou longa demais e foi cortada. Seja bem mais conciso e respeite os "
    "limites acima; responda SOMENTE com o objeto JSON."
)

_ALIASES = {
    "summary": ("summary", "resumo", "sumario", "sumário"),
    "notes": ("notes", "notas", "pontos", "key_points"),
    "tasks": ("tasks", "tarefas", "action_items", "acoes", "ações"),
    "topics": ("topics", "topicos", "tópicos", "temas"),
}
_TASK_TITLE = ("title", "titulo", "título", "task", "tarefa", "descricao", "descrição", "description")
_TASK_DUE = ("due", "prazo", "data", "deadline")
_TASK_OWNER = ("owner", "responsavel", "responsável", "assignee")

Build = Callable[[Limits], list[dict]]


def fields_for(kind: str) -> tuple[str, ...]:
    return FIELDS if kind == "all" else (kind,)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def prompt_tokens(messages: list[dict]) -> int:
    return sum(estimate_tokens(m["content"]) + MESSAGE_OVERHEAD for m in messages)


# ---------------------------------------------------------------- chunking
_SPLITTERS = (
    (re.compile(r"\n\s*\n"), "\n\n"),
    (re.compile(r"\n"), "\n"),
    (re.compile(r"(?<=[.!?…])\s+"), " "),
    (re.compile(r"\s+"), " "),
)


def _pieces(text: str, max_chars: int, level: int = 0) -> list[tuple[str, str]]:
    """(piece, joiner) units no longer than max_chars, splitting by paragraph > line > sentence > word."""
    if len(text) <= max_chars:
        return [(text, _SPLITTERS[max(level - 1, 0)][1])]
    if level >= len(_SPLITTERS):
        return [(text[i : i + max_chars], "") for i in range(0, len(text), max_chars)]
    pattern, joiner = _SPLITTERS[level]
    parts = [p for p in pattern.split(text) if p.strip()]
    if len(parts) <= 1:
        return _pieces(text, max_chars, level + 1)
    out: list[tuple[str, str]] = []
    for part in parts:
        sub = _pieces(part, max_chars, level + 1)
        out.extend((piece, joiner if i == 0 else j) for i, (piece, j) in enumerate(sub))
    return out


def split_text(text: str, max_tokens: int) -> list[str]:
    """Greedy packing of natural units into chunks of at most `max_tokens` (estimated)."""
    text = text.strip()
    if not text:
        return []
    max_chars = max(1, int(max_tokens * CHARS_PER_TOKEN))
    chunks: list[str] = []
    current = ""
    for piece, joiner in _pieces(text, max_chars):
        candidate = f"{current}{joiner}{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------- normalization
def _first(obj: dict, keys: tuple[str, ...]):
    for key in keys:
        if key in obj and obj[key] not in (None, ""):
            return obj[key]
    return None


def _str_list(value, cap: int) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [line.strip(" -•*\t") for line in value.splitlines()]
    out, seen = [], set()
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, dict):
            item = _first(item, ("text", "note", "topic", "title", "nome")) or ""
        text = str(item).strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:cap]


def _tasks(value, cap: int) -> list[dict]:
    if value is None:
        return []
    out, seen = [], set()
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = str(_first(item, _TASK_TITLE) or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        task = {"title": title}
        for key, aliases in (("due", _TASK_DUE), ("owner", _TASK_OWNER)):
            val = _first(item, aliases)
            if val is not None and str(val).strip() and str(val).strip().lower() not in ("null", "none", "n/a", "-"):
                task[key] = str(val).strip()
        out.append(task)
    return out[:cap]


def normalize(obj: dict, kind: str, limits: Limits = REMOTE_LIMITS["reduce"]) -> dict:
    """Coerce a model answer into {summary, notes, tasks, topics}; raises if it has none of the asked fields."""
    wanted = fields_for(kind)
    if not any(key in obj for f in wanted for key in _ALIASES[f]):
        raise JSONExtractionError(f"JSON sem os campos pedidos ({', '.join(wanted)})")
    summary = _first(obj, _ALIASES["summary"]) if "summary" in wanted else None
    if isinstance(summary, list):
        summary = "\n\n".join(str(s) for s in summary)
    return {
        "summary": str(summary or "").strip(),
        "notes": _str_list(_first(obj, _ALIASES["notes"]), limits.notes) if "notes" in wanted else [],
        "tasks": _tasks(_first(obj, _ALIASES["tasks"]), limits.tasks) if "tasks" in wanted else [],
        "topics": _str_list(_first(obj, _ALIASES["topics"]), limits.topics) if "topics" in wanted else [],
    }


def empty_result() -> dict:
    return {"summary": "", "notes": [], "tasks": [], "topics": []}


# ---------------------------------------------------------------- prompts
def _format_spec(kind: str, limits: Limits) -> str:
    wanted = fields_for(kind)
    values = {**asdict(limits), "note_words": NOTE_WORDS, "task_words": TASK_WORDS, "topic_words": TOPIC_WORDS}
    example = {k: v for k, v in EXAMPLE.items() if k in wanted}
    lines = "\n".join(f"- {FIELD_SPECS[f].format(**values)}" for f in wanted)
    return (
        f"{lines}\nFormato exato: {json.dumps(example, ensure_ascii=False)}\n"
        "Se não houver itens, use lista vazia; se houver mais que o limite, fique com os mais importantes."
    )


def map_messages(chunk: str, index: int, total: int, kind: str, limits: Limits) -> list[dict]:
    head = (
        "Analise o texto abaixo e extraia:"
        if total == 1
        else (f"Este é o trecho {index} de {total} de um texto maior. Extraia apenas o que está neste trecho:")
    )
    user = f'{head}\n{_format_spec(kind, limits)}\n\nTEXTO:\n"""\n{chunk}\n"""'
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def reduce_messages(partials: list[dict], kind: str, limits: Limits) -> list[dict]:
    user = (
        "Abaixo estão extrações parciais (JSON) de trechos consecutivos do MESMO texto. Combine-as em um único "
        "resultado final: o resumo deve cobrir o texto inteiro de forma coesa (não trecho por trecho); una "
        "notas, tarefas e temas, removendo duplicatas e mantendo prazos e responsáveis quando existirem.\n"
        f"{_format_spec(kind, limits)}\n\nPARCIAIS:\n{_compact(partials)}"
    )
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def with_note(messages: list[dict], note: str) -> list[dict]:
    """Same prompt with a note appended to the last user turn (no extra turns: the retry fits the same context)."""
    *head, last = messages
    return [*head, {**last, "content": f"{last['content']}\n\n{note}"}]


def _compact(partials: list[dict]) -> str:
    return "\n".join(json.dumps(p, ensure_ascii=False, separators=(",", ":")) for p in partials)


def _batches(partials: list[dict], max_tokens: int) -> list[list[dict]]:
    """Consecutive groups whose partials fit `max_tokens`; a group closes only once it has two items, so
    reduction converges. A trailing single partial stays alone (the caller passes it to the next round)."""
    groups: list[list[dict]] = []
    current: list[dict] = []
    for p in partials:
        if len(current) >= 2 and estimate_tokens(_compact([*current, p])) > max_tokens:
            groups.append(current)
            current = []
        current.append(p)
    if current:
        groups.append(current)
    return groups


# ---------------------------------------------------------------- engine
@dataclass
class Run:
    primary: str
    fallback: str | None
    primary_down: bool = False
    calls: int = 0
    models_used: list[str] = field(default_factory=list)

    def models(self) -> list[str]:
        return list(dict.fromkeys(m for m in (None if self.primary_down else self.primary, self.fallback) if m))


class Summarizer:
    def __init__(
        self,
        llm: LLMClient,
        *,
        default_model: str,
        fallback_model: str | None,
        map_chunk_tokens: int,
        fallback_chunk_tokens: int,
        local_max_tokens: int,
    ) -> None:
        self.llm = llm
        self.default_model = default_model
        self.fallback_model = fallback_model
        self.map_chunk_tokens = map_chunk_tokens
        self.fallback_chunk_tokens = fallback_chunk_tokens
        self.local_max_tokens = local_max_tokens

    @staticmethod
    def is_local(model: str) -> bool:
        return model.startswith("local-")

    def limits(self, model: str, phase: str) -> Limits:
        return (LOCAL_LIMITS if self.is_local(model) else REMOTE_LIMITS)[phase]

    def chunk_tokens(self, model: str) -> int:
        return self.map_chunk_tokens if self.is_local(model) else self.fallback_chunk_tokens

    def context_tokens(self, model: str) -> int:
        """Prompt + answer budget of one call, set by the largest map call (a full chunk)."""
        return self.chunk_tokens(model) + PROMPT_RESERVE + self.limits(model, "map").max_tokens

    def partials_budget(self, model: str) -> int:
        """Room for compact partials in one reduce prompt for `model`."""
        return max(200, self.context_tokens(model) - PROMPT_RESERVE - self.limits(model, "reduce").max_tokens)

    def summarize(self, text: str, kind: str = "all", model: str | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"kind inválido: {kind!r}")
        model = model or self.default_model
        tokens = estimate_tokens(text)
        if not text.strip():
            return {**empty_result(), "meta": self._meta(kind, model, Run(model, None), 0, 0)}
        huge = self.is_local(model) and tokens > self.local_max_tokens and self.fallback_model
        run = Run(primary=self.fallback_model if huge else model, fallback=None if huge else self.fallback_model)
        if huge:
            log.info(
                "text too large for the local model, using fallback", extra={"tokens_est": tokens, "model": run.primary}
            )
        chunks = split_text(text, self.chunk_tokens(run.primary))
        partials = [
            self._call(run, "map", functools.partial(map_messages, c, i, len(chunks), kind), kind)
            for i, c in enumerate(chunks, start=1)
        ]
        while len(partials) > 1:
            groups = _batches(partials, self.partials_budget(run.models()[0]))
            partials = [
                g[0] if len(g) == 1 else self._call(run, "reduce", functools.partial(reduce_messages, g, kind), kind)
                for g in groups
            ]
        result = partials[0]
        result["meta"] = self._meta(kind, model, run, len(chunks), tokens)
        return result

    @staticmethod
    def _meta(kind: str, model: str, run: Run, chunks: int, tokens: int) -> dict:
        return {
            "kind": kind,
            "model": model,
            "models_used": run.models_used,
            "chunks": chunks,
            "llm_calls": run.calls,
            "input_tokens_est": tokens,
            "fallback_used": any(m != model for m in run.models_used),
        }

    def _call(self, run: Run, phase: str, build: Build, kind: str) -> dict:
        models = run.models()
        last: Exception | None = None
        for position, model in enumerate(models):
            limits = self.limits(model, phase)
            if self.is_local(model) and position < len(models) - 1:
                needed = prompt_tokens(with_note(build(limits), RETRY_SHORTER)) + limits.max_tokens
                if needed > self.context_tokens(model):
                    log.info(
                        "prompt too large for the local model, using the fallback",
                        extra={"model": model, "phase": phase, "tokens_est": needed},
                    )
                    continue
            try:
                result = self._ask(run, model, build, limits, kind)
            except LLMError as exc:
                last = exc
                if model == run.primary and run.fallback:
                    run.primary_down = True  # don't wait on a dead/overloaded primary for every chunk
                log.warning("llm call failed", extra={"model": model, "error": str(exc)})
                continue
            except JSONExtractionError as exc:
                last = exc
                log.warning("llm returned no usable JSON", extra={"model": model, "error": str(exc)})
                continue
            if model not in run.models_used:
                run.models_used.append(model)
            return result
        raise UpstreamError(f"nenhum modelo produziu JSON válido ({', '.join(models)}): {last}")

    def _ask(self, run: Run, model: str, build: Build, limits: Limits, kind: str) -> dict:
        reply = self._chat(run, model, build(limits), limits)
        if reply.truncated:
            # A cut answer silently loses its last fields: ask again, from the same input, for less.
            log.info("answer cut by max_tokens, asking for a shorter one", extra={"model": model})
            limits = limits.shorter()
            reply = self._chat(run, model, with_note(build(limits), RETRY_SHORTER), limits, temperature=0.0)
        else:
            try:
                return normalize(extract_json_object(reply.content), kind, limits)
            except JSONExtractionError:
                reply = self._chat(run, model, with_note(build(limits), RETRY_JSON), limits, temperature=0.0)
        if reply.truncated:
            raise JSONExtractionError(f"resposta cortada em max_tokens={limits.max_tokens}")
        return normalize(extract_json_object(reply.content), kind, limits)

    def _chat(
        self, run: Run, model: str, messages: list[dict], limits: Limits, *, temperature: float = 0.2
    ) -> Completion:
        run.calls += 1
        return self.llm.chat(model, messages, max_tokens=limits.max_tokens, temperature=temperature)


def render_markdown(result: dict, title: str) -> str:
    lines = [f"# {title}", ""]
    if result.get("summary"):
        lines += ["## Resumo", "", result["summary"], ""]
    if result.get("notes"):
        lines += ["## Notas", ""] + [f"- {n}" for n in result["notes"]] + [""]
    if result.get("tasks"):
        lines += ["## Tarefas", ""]
        for t in result["tasks"]:
            extra = "; ".join(
                f"{label}: {t[k]}" for k, label in (("owner", "responsável"), ("due", "prazo")) if t.get(k)
            )
            lines.append(f"- [ ] {t['title']}" + (f" ({extra})" if extra else ""))
        lines.append("")
    if result.get("topics"):
        lines += ["## Tópicos", "", ", ".join(result["topics"]), ""]
    return "\n".join(lines).rstrip() + "\n"
