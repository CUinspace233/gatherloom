"""ProjectMaintenanceGraph: keep a project's summary current (product-plan §4.3).

load → gather → summarize → relate → draft_decisions → publish → finish

The job fixes a target watermark (the latest project event) when it starts
and stores every intermediate result in ``jobs.payload``, so a crash resumes
without repeating model calls. A newer event during the run queues another
job. ``publish`` writes a new summary version only if its watermark is newer
than the latest published one, so an old job never overwrites a newer
summary ([T4]). Nothing here writes project events.
"""

import itertools
from collections.abc import Callable
from typing import Annotated, Any, ClassVar, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import decisions, events, grounding, jobs, relations, search
from app.graphs import JobPipeline
from app.models import (
    Artifact,
    Chunk,
    Entry,
    Job,
    Project,
    ProjectEntry,
    ProjectEvent,
    ProjectQuestion,
    SummaryRevision,
)

PROMPT_VERSION = 1
TASK = "project_summary"
EVIDENCE_LIMIT = 16
EVIDENCE_CHARS = 600
CHANGED_ENTRIES = 8
PER_QUESTION = 3
FIRST_RUN_ENTRIES = 12
EMPTY = "项目里还没有已整理完成的资料。加入资料后，这里会自动总结当前认识。"

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class MaintenanceState(TypedDict):
    job_id: str
    owner_id: str
    project_id: str


class KeyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: Annotated[Text, StringConstraints(max_length=400)]
    citations: Annotated[list[int], Field(max_length=6)]


class SummaryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    understanding: Annotated[Text, StringConstraints(max_length=1500)]
    key_points: Annotated[list[KeyPoint], Field(max_length=8)]
    uncertainties: Annotated[
        list[Annotated[Text, StringConstraints(max_length=300)]], Field(max_length=6)
    ]
    change_note: Annotated[str, StringConstraints(max_length=300)]

    def cited(self) -> set[int]:
        return grounding.markers(self.understanding) | {
            n for point in self.key_points for n in point.citations
        }


SYSTEM = """你维护一个个人项目的“当前认识”总结。只能依据给出的材料写作。
规则：
1. 事实性陈述后用 [n] 标注证据编号；key_points 每条的 citations 必须与该条正文中的 [n] 完全一致。
2. 只有“正式决定”列表中的内容可以称为“已决定”。个人记录里写的“决定”“打算”，表述为“你记录过……”，不能说成已确认的决定。
3. 证据之间有矛盾时并列说明；同一件事前后变化时，以较新的记录为准并说明变化。
4. uncertainties 写目前仍不清楚、证据不足或相互矛盾的地方，可结合待解问题。
5. change_note 用一句话说明相对上一版总结的变化；没有上一版时写“首次总结”。
6. 不使用材料以外的知识，不编造来源。“导航上下文”只帮助理解，不能引用。
只输出 JSON：{"understanding": 段落, "key_points": [{"text": 要点, "citations": [编号]}], "uncertainties": [不确定项], "change_note": 变化说明}。"""


def validate_summary(content: str, numbers: set[int]) -> SummaryDraft:
    draft = grounding.parse(content, SummaryDraft)
    grounding.check_markers(
        [draft.understanding], grounding.markers(draft.understanding), numbers
    )
    for point in draft.key_points:
        grounding.check_markers([point.text], point.citations, numbers)
    return draft


def latest_summary(db: Session, project_id: UUID) -> SummaryRevision | None:
    return db.scalar(
        select(SummaryRevision)
        .where(SummaryRevision.project_id == project_id)
        .order_by(SummaryRevision.version.desc())
        .limit(1)
    )


class Pipeline(JobPipeline):
    """Maintenance jobs (kind ``project_maintenance``). Requires models."""

    PROMPT_VERSIONS: ClassVar[dict[str, int]] = {
        TASK: PROMPT_VERSION,
        relations.TASK: relations.PROMPT_VERSION,
        decisions.TASK: decisions.PROMPT_VERSION,
    }

    def initial_state(self, claim: jobs.Claim) -> dict[str, Any]:
        assert claim.project_id is not None
        return {
            "job_id": str(claim.id),
            "owner_id": str(claim.owner_id),
            "project_id": str(claim.project_id),
        }

    def build(self) -> StateGraph[Any]:
        graph = StateGraph(MaintenanceState)
        steps = [
            "load",
            "gather",
            "summarize",
            "relate",
            "draft_decisions",
            "publish",
            "finish",
        ]
        for name in steps:
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, steps[0])
        for current, following in itertools.pairwise(steps):
            graph.add_edge(current, following)
        graph.add_edge("finish", END)
        return graph

    # Helpers

    @property
    def project_id(self) -> UUID:
        assert self.claim is not None and self.claim.project_id is not None
        return self.claim.project_id

    def payload(self) -> dict[str, Any]:
        assert self.claim is not None
        with self.sessions() as db:
            return dict(db.get_one(Job, self.claim.id).payload or {})

    def save(self, stage: str, update: Callable[[dict[str, Any]], None]) -> None:
        with self.step(stage) as (_db, job):
            payload = dict(job.payload or {})
            update(payload)
            job.payload = payload

    # Nodes

    def load(self, state: MaintenanceState) -> dict[str, Any]:
        """Fix the target watermark for this run."""
        self.hook("load")
        if "watermark" in self.payload():
            return {}
        with self.sessions() as db:
            watermark = events.latest_seq(db, self.project_id)
            previous = latest_summary(db, self.project_id)
            since = previous.watermark if previous else 0

        def update(payload: dict[str, Any]) -> None:
            payload["watermark"] = watermark
            payload["since"] = since
            payload["noop"] = previous is not None and watermark <= previous.watermark

        self.save("load", update)
        return {}

    def gather(self, state: MaintenanceState) -> dict[str, Any]:
        """Collect numbered evidence: changed entries, then goal and questions."""
        self.hook("gather")
        payload = self.payload()
        if payload["noop"] or "evidence" in payload:
            return {}
        assert self.models is not None and self.claim is not None
        with self.sessions() as db:
            context = self.context(db, payload)
        self.save("gather", lambda p: p.update(context))
        return {}

    def context(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.models is not None and self.claim is not None
        owner_id, project_id = self.claim.owner_id, self.project_id
        project = db.get_one(Project, project_id)
        live = (
            select(Entry.id)
            .join(
                ProjectEntry,
                (ProjectEntry.entry_id == Entry.id)
                & (ProjectEntry.project_id == project_id),
            )
            .where(Entry.owner_id == owner_id, Entry.deleted_at.is_(None))
        )
        total = db.scalar(select(func.count()).select_from(live.subquery())) or 0
        changes = list(
            db.execute(
                select(ProjectEvent.kind, ProjectEvent.subject_id)
                .where(
                    ProjectEvent.project_id == project_id,
                    ProjectEvent.seq > payload["since"],
                    ProjectEvent.seq <= payload["watermark"],
                )
                .order_by(ProjectEvent.seq)
            )
        )
        changed = list(
            dict.fromkeys(
                subject
                for kind, subject in changes
                if kind in (events.ENTRY_READY, events.ENTRY_ADDED) and subject
            )
        )
        if payload["since"] == 0:
            changed += list(
                db.scalars(
                    live.order_by(Entry.created_at.desc()).limit(FIRST_RUN_ENTRIES)
                )
            )
        changed = [e for e in dict.fromkeys(changed) if e in set(db.scalars(live))]
        chosen: list[tuple[UUID, UUID]] = []  # (entry, chunk)
        changed_ready: list[UUID] = []
        for entry_id in changed[:CHANGED_ENTRIES]:
            chunk_id = db.scalar(
                select(Chunk.id)
                .join(
                    Entry,
                    (Entry.id == Chunk.entry_id)
                    & (Entry.current_revision == Chunk.revision),
                )
                .where(Chunk.entry_id == entry_id, Chunk.unit == "chunk")
                .order_by(Chunk.ordinal)
                .limit(1)
            )
            if chunk_id is not None:
                chosen.append((entry_id, chunk_id))
                changed_ready.append(entry_id)
        questions = list(
            db.scalars(
                select(ProjectQuestion)
                .where(
                    ProjectQuestion.project_id == project_id,
                    ProjectQuestion.status == "open",
                )
                .order_by(ProjectQuestion.created_at)
            )
        )
        for query in [project.goal, *(q.text for q in questions)]:
            if not query.strip():
                continue
            result = search.retrieve(
                db,
                owner_id,
                query,
                filters=search.Filters(project_id=project_id),
                embedder=self.models.embedder,
                record=self.record,
                max_distance=search.SEARCH_MAX_DISTANCE,
            )
            chosen += [
                (hit.entry_id, hit.chunk_id) for hit in result.hits[:PER_QUESTION]
            ]
        chosen = list(dict.fromkeys(chosen))
        used = chosen[:EVIDENCE_LIMIT]
        evidence = self.evidence(db, used)
        represented = {item["entry_id"] for item in evidence}
        ready = (
            db.scalar(
                select(func.count(func.distinct(Chunk.entry_id))).where(
                    Chunk.entry_id.in_(live), Chunk.unit == "chunk"
                )
            )
            or 0
        )
        return {
            "evidence": evidence,
            "changed_entries": [str(entry_id) for entry_id in changed_ready],
            "changes": [
                [kind, str(subject) if subject else None] for kind, subject in changes
            ],
            "questions": [{"id": str(q.id), "text": q.text} for q in questions],
            "coverage": {
                "entries": total,
                "ready": ready,
                "represented": len(represented),
                "partial": len(chosen) > len(used) or len(represented) < ready,
            },
        }

    def evidence(
        self, db: Session, chosen: list[tuple[UUID, UUID]]
    ) -> list[dict[str, Any]]:
        chunks = {
            chunk.id: chunk
            for chunk in db.scalars(
                select(Chunk).where(Chunk.id.in_([c for _e, c in chosen]))
            )
        }
        entries = {
            entry.id: entry
            for entry in db.scalars(
                select(Entry).where(Entry.id.in_([e for e, _c in chosen]))
            )
        }
        cards = {
            artifact.entry_id: artifact.content
            for artifact in db.scalars(
                select(Artifact).where(
                    Artifact.entry_id.in_(list(entries)), Artifact.kind == "card"
                )
            )
            if artifact.revision == entries[artifact.entry_id].current_revision
        }
        result: list[dict[str, Any]] = []
        for entry_id, chunk_id in chosen:
            chunk, entry = chunks[chunk_id], entries[entry_id]
            card = cards.get(entry_id) or {}
            result.append(
                {
                    "n": len(result) + 1,
                    "entry_id": str(entry_id),
                    "revision": chunk.revision,
                    "chunk_id": str(chunk_id),
                    "source": chunk.source,
                    "start_offset": chunk.start_offset,
                    "end_offset": chunk.end_offset,
                    "type": entry.type,
                    "title": str(card.get("title") or ""),
                    "summary": str(card.get("summary") or ""),
                    "created_at": entry.created_at.isoformat(),
                    "text": chunk.display_text[:EVIDENCE_CHARS],
                }
            )
        return result

    def prompt(self, db: Session, payload: dict[str, Any]) -> str:
        project = db.get_one(Project, self.project_id)
        previous = latest_summary(db, self.project_id)
        lines = [f"项目：{project.name}", f"用户目标：{project.goal or '（未填写）'}"]
        if payload["questions"]:
            lines.append("待解问题：")
            lines += [f"- {q['text']}" for q in payload["questions"]]
        lines.append("正式决定：")
        lines += self.decision_lines(db) or ["（暂无）"]
        if previous:
            lines.append("上一版总结（不可引用）：")
            lines.append(grounding.strip_markers(previous.content["understanding"]))
        lines.append(f"本期变化：{self.describe_changes(payload['changes'])}")
        lines.append("\n证据：")
        lines += grounding.evidence_block(payload["evidence"], EVIDENCE_CHARS)
        navigation = [
            f"- {item['title']}：{item['summary']}"
            for item in payload["evidence"]
            if item["title"] and item["summary"]
        ]
        if navigation:
            lines.append("\n导航上下文（不可引用）：")
            lines += list(dict.fromkeys(navigation))
        return "\n".join(lines)

    def decision_lines(self, db: Session) -> list[str]:
        return decisions.confirmed_lines(db, self.project_id)

    @staticmethod
    def describe_changes(changes: list[list[str | None]]) -> str:
        names = {
            events.ENTRY_READY: "资料整理完成",
            events.ENTRY_ADDED: "加入资料",
            events.ENTRY_REMOVED: "移出资料",
            events.ENTRY_DELETED: "删除资料",
            events.QUESTION_ADDED: "新增待解问题",
            events.QUESTION_CHANGED: "待解问题变化",
            events.DECISION_CONFIRMED: "确认决定",
            events.DECISION_REVISED: "修订决定",
            events.DECISION_SUPERSEDED: "决定被替代",
        }
        counts: dict[str, int] = {}
        for kind, _subject in changes:
            label = names.get(str(kind), str(kind))
            counts[label] = counts.get(label, 0) + 1
        return (
            "，".join(f"{label} {count} 次" for label, count in counts.items()) or "无"
        )

    def summarize(self, state: MaintenanceState) -> dict[str, Any]:
        self.hook("summarize")
        payload = self.payload()
        if payload["noop"] or "summary" in payload:
            return {}
        assert self.models is not None
        numbers = {item["n"] for item in payload["evidence"]}
        if not numbers:
            summary: dict[str, Any] = {
                "understanding": EMPTY,
                "key_points": [],
                "uncertainties": [],
                "change_note": "暂无资料",
            }
            model = None
        else:
            with self.sessions() as db:
                prompt = self.prompt(db, payload)
            draft, model = grounding.ask(
                self.models.router,
                "heavy",
                [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                TASK,
                self.record,
                lambda content: validate_summary(content, numbers),
            )
            summary = draft.model_dump()

        def update(p: dict[str, Any]) -> None:
            p["summary"] = summary
            p["model"] = model

        self.save("summarize", update)
        return {}

    def relate(self, state: MaintenanceState) -> dict[str, Any]:
        """Suggest related entries and questions for this run's changed entries."""
        self.hook("relate")
        payload = self.payload()
        if payload["noop"]:
            return {}
        assert self.models is not None and self.claim is not None
        done = set(payload.get("related", []))
        pending = [e for e in payload.get("changed_entries", []) if e not in done]
        if not pending:
            return {}
        owner_id, project_id = self.claim.owner_id, self.project_id
        with self.sessions() as db:
            questions = relations.embed_questions(
                db, project_id, self.models.embedder, self.record
            )
            for entry_text in pending:
                entry_id = UUID(entry_text)
                suggestions = (
                    []
                    if relations.already_suggested(db, project_id, entry_id)
                    else relations.suggest(
                        db,
                        owner_id,
                        project_id,
                        entry_id,
                        questions,
                        self.models.router,
                        self.record,
                    )
                )
                with self.step("relate") as (write, job):
                    relations.store(write, owner_id, project_id, entry_id, suggestions)
                    updated = dict(job.payload or {})
                    updated["related"] = [*updated.get("related", []), entry_text]
                    job.payload = updated
        return {}

    def draft_decisions(self, state: MaintenanceState) -> dict[str, Any]:
        """Propose decision drafts, only when this run saw new material."""
        self.hook("draft_decisions")
        payload = self.payload()
        new_material = any(
            kind in (events.ENTRY_READY, events.ENTRY_ADDED)
            for kind, _subject in payload.get("changes", [])
        )
        if payload["noop"] or payload.get("drafted") or not new_material:
            return {}
        assert self.models is not None and self.claim is not None
        owner_id, project_id = self.claim.owner_id, self.project_id
        with self.sessions() as db:
            drafts = decisions.propose(
                db,
                owner_id,
                project_id,
                payload["evidence"],
                self.models.router,
                self.record,
                self.clock(),
            )
        with self.step("draft_decisions") as (db, job):
            decisions.store(db, owner_id, project_id, drafts)
            updated = dict(job.payload or {})
            updated["drafted"] = True
            job.payload = updated
        return {}

    def publish(self, state: MaintenanceState) -> dict[str, Any]:
        """Write a new version unless a newer watermark is already published."""
        self.hook("publish")
        payload = self.payload()
        if payload["noop"]:
            return {}
        with self.step("publish") as (db, _job):
            latest = latest_summary(db, self.project_id)
            if latest is not None and latest.watermark >= payload["watermark"]:
                return {}
            summary = payload["summary"]
            cited = grounding.markers(summary["understanding"]) | {
                n for point in summary["key_points"] for n in point["citations"]
            }
            assert self.claim is not None
            db.add(
                SummaryRevision(
                    owner_id=self.claim.owner_id,
                    project_id=self.project_id,
                    version=(latest.version if latest else 0) + 1,
                    content={
                        key: summary[key]
                        for key in ("understanding", "key_points", "uncertainties")
                    },
                    sources=[i for i in payload["evidence"] if i["n"] in cited],
                    watermark=payload["watermark"],
                    coverage=payload["coverage"],
                    change_note=summary["change_note"],
                    model=payload.get("model"),
                )
            )
        return {}
