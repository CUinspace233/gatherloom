"""ProjectAnswerGraph: answer a question from the project's own material.

load → retrieve → answer → publish → finish

- retrieve searches only the conversation's project and stores the evidence
  (numbered source chunks) and navigation context (knowledge cards, never
  numbered, so never citable) in the question's ``trace``.
- answer calls the heavy model once, validates citations against the
  evidence numbers, repairs once, and stores the validated draft in the trace
  so a crash before publishing does not call the model again.
- publish writes the assistant message and its citations; ``reply_to`` is
  unique, so replays cannot create a second answer.

With no evidence at all the answer is a fixed "insufficient" reply and no
model is called. No web access, no query rewriting, one retrieval round.
"""

from typing import Annotated, Any, ClassVar, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import grounding, jobs, search
from app.graphs import JobPipeline
from app.models import Artifact, Chunk, Citation, Conversation, Entry, Message, Project

PROMPT_VERSION = 2
TASK = "project_answer"
EVIDENCE = 6
HISTORY = 6
EVIDENCE_CHARS = 1200
INSUFFICIENT = "现有资料不足以回答这个问题。可以先收集相关资料，或换个问法再试。"


class AnswerState(TypedDict):
    job_id: str
    owner_id: str
    message_id: str


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    citations: Annotated[list[int], Field(max_length=12)]
    insufficient: bool


SYSTEM = """你是个人项目知识库的问答助手，只能依据用户提供的证据回答。
规则：
1. 每个事实性陈述后用 [n] 标注证据编号，n 必须是证据列表里的编号。
2. 证据分“外部资料”和“个人记录”。引用个人记录时说明这是用户自己记录的想法或决定。
3. “导航上下文”是 AI 整理的资料摘要，只帮助你理解，不能引用，也不能当作事实来源。
4. 证据之间有矛盾时并列说明各自的依据；同一件事的决定前后变化时，以较新的记录为准并说明变化。
5. 证据不足以完整回答时，insufficient 为 true，说明缺少什么；只有与问题直接相关的部分证据才引用并标注 [n]，不要罗列证据涵盖了哪些主题，不要猜测。
6. 不使用证据以外的知识补充事实，不编造来源。
只输出 JSON：{"answer": 回答正文, "citations": [用到的编号], "insufficient": true 或 false}。"""


def validate(content: str, numbers: set[int]) -> Draft:
    """Parse and check a reply. Raises ValueError with a message for repair.

    An insufficient answer may cite the partly relevant evidence it used; a
    sufficient one must cite something.
    """
    draft = grounding.parse(content, Draft)
    draft.answer, draft.citations = grounding.reconcile(draft.answer, draft.citations)
    grounding.check_markers([draft.answer], draft.citations, numbers)
    if not draft.insufficient and not draft.citations:
        raise ValueError("回答没有引用任何证据；证据不足时应将 insufficient 设为 true")
    return draft


def user_prompt(
    project: Project,
    trace: dict[str, Any],
    history: list[tuple[str, str]],
    question: str,
) -> str:
    lines = [f"项目：{project.name}"]
    if project.goal:
        lines.append(f"项目目标：{project.goal}")
    lines.append("\n证据：")
    lines += grounding.evidence_block(trace["evidence"], EVIDENCE_CHARS)
    if trace["navigation"]:
        lines.append("\n导航上下文（不可引用）：")
        lines += [
            f"- {card['title']}：{card['summary']}" for card in trace["navigation"]
        ]
    if history:
        lines.append("\n之前的对话：")
        lines += [
            f"{'用户' if role == 'user' else '助手'}：{text}" for role, text in history
        ]
    lines.append(f"\n问题：{question}")
    return "\n".join(lines)


class Pipeline(JobPipeline):
    """Answer jobs (kind ``project_answer``). Requires models."""

    PROMPT_VERSIONS: ClassVar[dict[str, int]] = {TASK: PROMPT_VERSION}

    def initial_state(self, claim: jobs.Claim) -> dict[str, Any]:
        assert claim.message_id is not None
        return {
            "job_id": str(claim.id),
            "owner_id": str(claim.owner_id),
            "message_id": str(claim.message_id),
        }

    def build(self) -> StateGraph[Any]:
        graph = StateGraph(AnswerState)
        for name in ("load", "retrieve", "answer", "publish", "finish"):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "load")
        graph.add_edge("load", "retrieve")
        graph.add_edge("retrieve", "answer")
        graph.add_edge("answer", "publish")
        graph.add_edge("publish", "finish")
        graph.add_edge("finish", END)
        return graph

    def question(self, db: Session) -> tuple[Message, Conversation, Project]:
        assert self.claim is not None
        message = db.get_one(Message, self.claim.message_id)
        conversation = db.get_one(Conversation, message.conversation_id)
        project = db.scalars(
            select(Project).where(
                Project.owner_id == self.claim.owner_id,
                Project.id == conversation.project_id,
            )
        ).one()
        return message, conversation, project

    def load(self, state: AnswerState) -> dict[str, Any]:
        self.hook("load")
        with self.step("load") as (db, _job):
            message, _conversation, _project = self.question(db)
            if message.role != "user" or message.owner_id != self.claim_owner():
                raise jobs.Permanent("not_a_question")
        return {}

    def claim_owner(self) -> UUID:
        assert self.claim is not None
        return self.claim.owner_id

    def retrieve(self, state: AnswerState) -> dict[str, Any]:
        """Search the project and store numbered evidence in the trace."""
        self.hook("retrieve")
        assert self.models is not None
        with self.sessions() as db:
            message, conversation, _project = self.question(db)
            if message.trace and "evidence" in message.trace:
                return {}
            result = search.retrieve(
                db,
                self.claim_owner(),
                message.content,
                filters=search.Filters(project_id=conversation.project_id),
                embedder=self.models.embedder,
                record=self.record,
            )
            trace = self.evidence(db, result)
        with self.step("retrieve") as (db, _job):
            message = db.get_one(Message, message.id)
            message.trace = trace
        return {}

    def evidence(self, db: Session, result: search.SearchResult) -> dict[str, Any]:
        hits = result.hits[:EVIDENCE]
        entries = {
            entry.id: entry
            for entry in db.scalars(
                select(Entry).where(Entry.id.in_([hit.entry_id for hit in hits]))
            )
        }
        chunks = {
            chunk.id: chunk
            for chunk in db.scalars(
                select(Chunk).where(Chunk.id.in_([hit.chunk_id for hit in hits]))
            )
        }
        cards = {
            artifact.entry_id: artifact.content
            for artifact in db.scalars(
                select(Artifact).where(
                    Artifact.entry_id.in_(list(entries)),
                    Artifact.kind == "card",
                )
            )
            if artifact.revision == entries[artifact.entry_id].current_revision
        }
        evidence: list[dict[str, Any]] = []
        for n, hit in enumerate(hits, start=1):
            entry, chunk = entries[hit.entry_id], chunks[hit.chunk_id]
            card = cards.get(entry.id) or {}
            evidence.append(
                {
                    "n": n,
                    "entry_id": str(entry.id),
                    "revision": chunk.revision,
                    "chunk_id": str(chunk.id),
                    "type": entry.type,
                    "title": str(card.get("title") or ""),
                    "created_at": entry.created_at.isoformat(),
                    "source": chunk.source,
                    "start_offset": chunk.start_offset,
                    "end_offset": chunk.end_offset,
                    "text": chunk.display_text,
                    "score": round(hit.score, 6),
                    "matched_by": sorted(hit.matched_by),
                }
            )
        navigation = [
            {
                "entry_id": str(entry_id),
                "title": found["title"],
                "summary": found["summary"],
            }
            for entry_id in dict.fromkeys(hit.entry_id for hit in hits)
            if (found := cards.get(entry_id))
        ]
        return {
            "query_terms": result.terms,
            "degraded": result.degraded,
            "evidence": evidence,
            "navigation": navigation,
        }

    def answer(self, state: AnswerState) -> dict[str, Any]:
        """Call the heavy model once (plus one repair) and keep the draft."""
        self.hook("answer")
        assert self.models is not None
        with self.sessions() as db:
            message, conversation, project = self.question(db)
            trace = dict(message.trace or {})
            if "draft" in trace:
                return {}
            numbers = {item["n"] for item in trace["evidence"]}
            if not numbers:
                draft = {"answer": INSUFFICIENT, "citations": [], "insufficient": True}
                model = None
            else:
                history = [
                    (role, grounding.strip_markers(content))
                    for role, content in db.execute(
                        select(Message.role, Message.content)
                        .where(
                            Message.conversation_id == conversation.id,
                            Message.created_at < message.created_at,
                        )
                        .order_by(Message.created_at.desc())
                        .limit(HISTORY)
                    )
                ][::-1]
                prompt = user_prompt(project, trace, history, message.content)
                validated, model = self.ask(prompt, numbers)
                draft = validated.model_dump()
        with self.step("answer") as (db, _job):
            message = db.get_one(Message, message.id)
            message.trace = {**trace, "draft": draft, "model": model}
        return {}

    def ask(self, prompt: str, numbers: set[int]) -> tuple[Draft, str]:
        assert self.models is not None
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ]
        return grounding.ask(
            self.models.router,
            "heavy",
            messages,
            TASK,
            self.record,
            lambda content: validate(content, numbers),
        )

    def publish(self, state: AnswerState) -> dict[str, Any]:
        """Write the answer and its citations once."""
        self.hook("publish")
        with self.step("publish") as (db, _job):
            question, conversation, _project = self.question(db)
            trace = question.trace or {}
            draft = trace["draft"]
            answer_id = db.scalar(
                insert(Message)
                .values(
                    owner_id=question.owner_id,
                    conversation_id=question.conversation_id,
                    role="assistant",
                    content=draft["answer"],
                    reply_to=question.id,
                    insufficient=draft["insufficient"],
                )
                .on_conflict_do_nothing(constraint="uq_messages_reply_to")
                .returning(Message.id)
            )
            if answer_id is not None:
                evidence = {item["n"]: item for item in trace["evidence"]}
                for n in sorted(set(draft["citations"])):
                    item = evidence[n]
                    db.add(
                        Citation(
                            owner_id=question.owner_id,
                            message_id=answer_id,
                            n=n,
                            entry_id=UUID(item["entry_id"]),
                            revision=item["revision"],
                            chunk_id=UUID(item["chunk_id"]),
                            source=item.get("source", "text"),
                            start_offset=item["start_offset"],
                            end_offset=item["end_offset"],
                            snippet=item["text"],
                        )
                    )
                conversation.updated_at = self.clock()
        return {}
