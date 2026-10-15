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

from zchut.answer.citations import display_hits, render
from zchut.answer.prompt import SYSTEM_PROMPT
from zchut.answer.refusal import REFUSAL_SENTENCE, should_refuse
from zchut.graph.clarifications import Clarifications, applicable
from zchut.graph.classify import Classifier
from zchut.graph.condense import NUDGE_LINE, Condenser, Turns
from zchut.graph.offices import Office, escalation_text, office_line
from zchut.graph.state import HISTORY_CAP, ConversationState, Session, Topic, Verdict
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
    condenser: Condenser
    verifier: Verifier | None
    offices: dict[Topic, Office]
    clarifications: Clarifications
    context_chars: int
    tier: str = "haiku"
    retriever: Retriever | None = None


def turns_of(deps: GraphDeps, s: ConversationState) -> Turns:
    asked = deps.clarifications.get(s.pending_fact or "")
    question = asked.question_he if asked else None
    return Turns(s.history, s.question, s.pending_clarification, question)


def condensing(s: ConversationState) -> bool:
    return bool(s.history or s.pending_clarification)


def plan_usd(deps: GraphDeps, guard: CostGuard, question: str, turns: Turns | None) -> float:
    q = estimate_tokens(question)
    context = q + estimate_tokens(SYSTEM_PROMPT) + deps.context_chars // 2
    answer, condense = deps.ask.model, deps.condenser
    calls = (
        [(condense.model, estimate_tokens(turns.prompt()), condense.max_tokens)] if turns else []
    )
    calls += [
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

    def condense(s: ConversationState) -> Update:
        c = deps.condenser.condense(turns_of(deps, s), guard)
        folded = c.is_reply_to_clarification and s.pending_clarification is not None
        return {
            "standalone_question": c.standalone_question,
            "is_question": c.is_question or folded,
            "clarified": folded,
        }

    def classify(s: ConversationState) -> Update:
        c = deps.classifier.classify(s.standalone, guard)
        return {
            "topic": c.topic,
            "in_scope": c.in_scope,
            "missing_fact": applicable(c.missing_fact, c.topic, deps.clarifications),
            "office_lookup": c.office_lookup and c.topic != "other",
        }

    def clarify(s: ConversationState) -> Update:
        assert s.missing_fact is not None
        reply = str(interrupt(deps.clarifications[s.missing_fact].question_he))
        return {
            "pending_clarification": s.standalone,
            "pending_fact": s.missing_fact,
            "question": reply,
            "can_clarify": False,
        }

    def retrieve_hits(s: ConversationState) -> Update:
        search = deps.retriever or (lambda q, g: retrieve(q, deps.ask, g))
        return {"hits": search(s.standalone, guard)}

    def answer(s: ConversationState) -> Update:
        return {"draft": respond(s.standalone, s.hits, deps.ask, guard)}

    def verify(s: ConversationState) -> Update:
        assert deps.verifier is not None and s.draft is not None
        hits = display_hits(s.draft, s.hits)
        return {"verdict": deps.verifier.verify(s.draft.text, hits, guard)}

    def final(s: ConversationState) -> Update:
        assert s.draft is not None
        return {"final": render(s.draft)}

    def escalate(s: ConversationState) -> Update:
        office = deps.offices[s.topic or "other"]
        if s.office_lookup:
            return {"final": office_line(office)}
        text = escalation_text(office, s.verdict.unsupported_claims if s.verdict else [])
        return {"escalation": text, "final": text}

    def refuse(s: ConversationState) -> Update:
        return {"final": REFUSAL_SENTENCE}

    def nudge(s: ConversationState) -> Update:
        return {"final": NUDGE_LINE}

    def after_start(s: ConversationState) -> str:
        return "condense" if condensing(s) else "classify"

    def after_condense(s: ConversationState) -> str:
        return "classify" if s.is_question else "nudge"

    def after_classify(s: ConversationState) -> str:
        if not s.in_scope:
            return "refuse"
        if s.office_lookup:
            return "escalate"
        return "clarify" if s.missing_fact and s.can_clarify else "retrieve"

    def after_retrieve(s: ConversationState) -> str:
        return "refuse" if should_refuse(s.hits, deps.ask.min_score) else "answer"

    def after_verify(s: ConversationState) -> str:
        return "final" if s.verdict and s.verdict.supported else "escalate"

    steps = {
        "condense": condense,
        "classify": classify,
        "clarify": clarify,
        "retrieve": retrieve_hits,
        "answer": answer,
        "verify": verify,
        "final": final,
        "escalate": escalate,
        "refuse": refuse,
        "nudge": nudge,
    }
    graph = StateGraph(ConversationState)
    for name, step in steps.items():
        graph.add_node(name, node(name, step))
    graph.add_conditional_edges(START, after_start, ["condense", "classify"])
    graph.add_conditional_edges("condense", after_condense, ["classify", "nudge"])
    graph.add_conditional_edges(
        "classify", after_classify, ["refuse", "escalate", "clarify", "retrieve"]
    )
    graph.add_edge("clarify", "condense")
    graph.add_conditional_edges("retrieve", after_retrieve, ["refuse", "answer"])
    graph.add_edge("answer", "verify" if deps.verifier is not None else "final")
    graph.add_conditional_edges("verify", after_verify, ["final", "escalate"])
    for end in ("final", "escalate", "refuse", "nudge"):
        graph.add_edge(end, END)
    return graph.compile(checkpointer=InMemorySaver(serde=_SERDE))


def converse(
    deps: GraphDeps,
    guard: CostGuard,
    question: str,
    history: list[str],
    reply: Reply | None,
    *,
    pause: bool = False,
    session: Session | None = None,
) -> ConversationState:
    # pause: stop at the interrupt; session: a pending clarification and the once-per-session flag.
    s = session or Session()
    start = ConversationState(
        question=question,
        history=history[-HISTORY_CAP:],
        pending_clarification=s.pending_clarification,
        pending_fact=s.pending_fact,
        can_clarify=(reply is not None or pause) and not s.clarified_once,
    )
    turns = turns_of(deps, start) if condensing(start) else None
    guard.reserve(plan_usd(deps, guard, question, turns))
    app = build_graph(deps, guard)
    config: RunnableConfig = {"configurable": {"thread_id": uuid4().hex}}
    out = app.invoke(start, config)
    if reply is not None and "__interrupt__" in out:
        out = app.invoke(Command(resume=reply(str(out["__interrupt__"][0].value))), config)
    return ConversationState.model_validate(out)
