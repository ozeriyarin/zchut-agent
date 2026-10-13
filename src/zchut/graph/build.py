from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from zchut.answer.citations import render
from zchut.answer.prompt import SYSTEM_PROMPT
from zchut.answer.refusal import REFUSAL_SENTENCE, should_refuse
from zchut.graph.classify import Classifier
from zchut.graph.offices import Office, escalation_text
from zchut.graph.state import HISTORY_CAP, ConversationState, Topic, Verdict
from zchut.graph.verify import Verifier
from zchut.run.ask import AskDeps, respond, retrieve
from zchut.run.cost_guard import CostGuard, cost_usd
from zchut.tokens import estimate_tokens
from zchut.types import Answer, CallRecord, Chunk, Citation, Hit, Usage

Retriever = Callable[[str, CostGuard], list[Hit]]
Reply = Callable[[str], str]
Update = dict[str, object]
Graph = CompiledStateGraph[ConversationState, None, ConversationState, ConversationState]
_SERDE = JsonPlusSerializer(
    allowed_msgpack_modules=[Hit, Chunk, Answer, Citation, CallRecord, Verdict]
)


@dataclass(frozen=True)
class GraphDeps:
    ask: AskDeps
    classifier: Classifier
    verifier: Verifier | None
    offices: dict[Topic, Office]
    context_chars: int
    tier: str = "haiku"
    retriever: Retriever | None = None


def plan_usd(deps: GraphDeps, guard: CostGuard, question: str) -> float:
    q = estimate_tokens(question)
    context = q + estimate_tokens(SYSTEM_PROMPT) + deps.context_chars // 2
    answer = deps.ask.model
    calls = [
        (deps.classifier.model, q, deps.classifier.max_tokens),
        (deps.ask.embedder.model, q, 0),
        (answer.model, context, answer.max_tokens),
    ]
    if deps.verifier is not None:
        verify_in = context + answer.max_tokens
        calls += [(m, verify_in, deps.verifier.max_tokens) for m in deps.verifier.models]
    return sum(cost_usd(guard.prices, m, Usage(i, o)) for m, i, o in calls)


def build_graph(deps: GraphDeps, guard: CostGuard) -> Graph:
    calls = guard.recorder.record.calls

    def node(name: str, step: Callable[[ConversationState], Update]) -> Callable[..., Update]:
        def run(state: ConversationState) -> Update:
            before = len(calls)
            update = step(state)
            return {**update, "path": [name], "calls": calls[before:]}

        return run

    def classify(s: ConversationState) -> Update:
        return deps.classifier.classify(s.conversation(), guard).model_dump()

    def clarify(s: ConversationState) -> Update:
        reply = str(interrupt(s.missing_fact))
        return {"history": [*s.history, reply][-HISTORY_CAP:], "clarified": True}

    def retrieve_hits(s: ConversationState) -> Update:
        search = deps.retriever or (lambda q, g: retrieve(q, deps.ask, g))
        return {"hits": search(s.query(), guard)}

    def answer(s: ConversationState) -> Update:
        return {"draft": respond(s.query(), s.hits, deps.ask, guard)}

    def verify(s: ConversationState) -> Update:
        assert deps.verifier is not None and s.draft is not None
        return {"verdict": deps.verifier.verify(s.draft.text, s.hits, guard)}

    def final(s: ConversationState) -> Update:
        assert s.draft is not None
        return {"final": render(s.draft)}

    def escalate(s: ConversationState) -> Update:
        claims = s.verdict.unsupported_claims if s.verdict else []
        text = escalation_text(deps.offices[s.topic or "other"], claims)
        return {"escalation": text, "final": text}

    def refuse(s: ConversationState) -> Update:
        return {"final": REFUSAL_SENTENCE}

    def after_classify(s: ConversationState) -> str:
        if not s.in_scope:
            return "refuse"
        return "clarify" if s.missing_fact and s.can_clarify and not s.clarified else "retrieve"

    def after_retrieve(s: ConversationState) -> str:
        return "refuse" if should_refuse(s.hits, deps.ask.min_score) else "answer"

    def after_verify(s: ConversationState) -> str:
        return "final" if s.verdict and s.verdict.supported else "escalate"

    steps = {
        "classify": classify,
        "clarify": clarify,
        "retrieve": retrieve_hits,
        "answer": answer,
        "verify": verify,
        "final": final,
        "escalate": escalate,
        "refuse": refuse,
    }
    graph = StateGraph(ConversationState)
    for name, step in steps.items():
        graph.add_node(name, node(name, step))
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", after_classify, ["refuse", "clarify", "retrieve"])
    graph.add_edge("clarify", "classify")
    graph.add_conditional_edges("retrieve", after_retrieve, ["refuse", "answer"])
    graph.add_edge("answer", "verify" if deps.verifier is not None else "final")
    graph.add_conditional_edges("verify", after_verify, ["final", "escalate"])
    for end in ("final", "escalate", "refuse"):
        graph.add_edge(end, END)
    return graph.compile(checkpointer=InMemorySaver(serde=_SERDE))


def converse(
    deps: GraphDeps, guard: CostGuard, question: str, history: list[str], reply: Reply | None
) -> ConversationState:
    guard.reserve(plan_usd(deps, guard, question))
    app = build_graph(deps, guard)
    config: RunnableConfig = {"configurable": {"thread_id": uuid4().hex}}
    start = ConversationState(
        question=question, history=history[-HISTORY_CAP:], can_clarify=reply is not None
    )
    out = app.invoke(start, config)
    if reply is not None and "__interrupt__" in out:
        out = app.invoke(Command(resume=reply(str(out["__interrupt__"][0].value))), config)
    return ConversationState.model_validate(out)
