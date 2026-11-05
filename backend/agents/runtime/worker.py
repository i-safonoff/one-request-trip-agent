"""Agents worker: consumes `agent_runs` and runs the orchestrator. The agents side of the chat <-> agents contract
(chat/README.md):

    python -m agents.runtime.worker

* one `running` run per chat (partial unique index), runs of a chat are processed in creation order;
* the user's message goes `queued -> processing -> done | failed`;
* a question for the user is a `clarifications` row + `clarification.required`; the run becomes
  `waiting_clarification` and does not hold the queue; the answer arrives as a new run;
* the plan is written to `trip_plans` by the store; the final answer is an assistant message + `assistant.message`;
* a run whose heartbeat stops (worker crash) goes back to `queued` and resumes from its checkpoint.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import traceback
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from shared.commands import ApplyUserAction, ProcessClarificationAnswer, ProcessUserMessage, agent_command_adapter
from shared.database import Database, get_database
from shared.events import (AssistantMessage, ClarificationRequired, EventType, RequestFailed, emit_event)
from shared.models import AgentRun, ChatMessage, Clarification

from ..config import get_settings
from ..domain.turn import AgentTurn
from ..integrations.llm import LLMError
from .db_store import DbPlanStore

log = logging.getLogger("worker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Worker:
    def __init__(self, db: Database, make_orchestrator, store: DbPlanStore, stale_seconds: int = 60, poll: float = 1.0, deadline: float = 150.0):
        self.db, self._make, self.store = db, make_orchestrator, store
        self.stale_seconds, self.poll, self.deadline = stale_seconds, poll, deadline
        self._orch = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def orch(self):
        """Built on first use: a missing GIGACHAT_AUTH_KEY fails the run with a clear message, not the whole worker."""
        if self._orch is None:
            self._orch = self._make()
        return self._orch

    # ------------------------------------------------------------------ queue
    async def loop(self) -> None:
        log.info("worker started")
        while not self._stop.is_set():
            try:
                await self.requeue_stale()
                run = await self.claim()
            except Exception:  # noqa: BLE001
                log.exception("queue error")
                await asyncio.sleep(2)
                continue
            if run is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll)
                except asyncio.TimeoutError:
                    pass
                continue
            await self.process(run)

    async def requeue_stale(self) -> None:
        limit = _now() - timedelta(seconds=self.stale_seconds)
        async with self.db.sessionmaker() as s:
            for run in await s.scalars(select(AgentRun).where(AgentRun.status == "running", AgentRun.heartbeat_at < limit)):
                if run.attempts >= 3:
                    run.status, run.error, run.finished_at = "failed", "worker stopped responding", _now()
                else:
                    run.status = "queued"
            await s.commit()

    async def claim(self) -> uuid.UUID | None:
        """Take the oldest queued run of a chat that has no running run. Returns its id."""
        async with self.db.sessionmaker() as s:
            busy = select(AgentRun.chat_id).where(AgentRun.status == "running")
            run = await s.scalar(select(AgentRun).where(AgentRun.status == "queued", AgentRun.chat_id.not_in(busy))
                                 .order_by(AgentRun.created_at).limit(1).with_for_update(skip_locked=True))
            if run is None:
                return None
            run.status, run.attempts = "running", (run.attempts or 0) + 1
            run.started_at, run.heartbeat_at = run.started_at or _now(), _now()
            run_id = run.id
            try:
                await s.commit()
            except IntegrityError:  # another worker took this chat a moment earlier
                await s.rollback()
                return None
        return run_id

    async def _heartbeat(self, run_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(10)
            async with self.db.sessionmaker() as s:
                await s.execute(update(AgentRun).where(AgentRun.id == run_id).values(heartbeat_at=_now()))
                await s.commit()

    # ------------------------------------------------------------------ one run
    async def process(self, run_id: uuid.UUID) -> None:
        hb = asyncio.create_task(self._heartbeat(run_id))
        message_id = None
        try:
            async with self.db.sessionmaker() as s:
                run = await s.get(AgentRun, run_id)
                chat_id, command = run.chat_id, agent_command_adapter.validate_python(run.command)
                if isinstance(command, (ProcessUserMessage, ProcessClarificationAnswer)):
                    message_id = command.message_id
                    msg = await s.get(ChatMessage, message_id)
                    msg.status, text = "processing", msg.text
                if isinstance(command, ProcessClarificationAnswer):  # this run supersedes the one that was waiting
                    for old in await s.scalars(select(AgentRun).where(AgentRun.chat_id == chat_id, AgentRun.id != run_id,
                                                                      AgentRun.status == "waiting_clarification")):
                        old.status, old.finished_at = "done", _now()
                hist = (await s.scalars(select(ChatMessage).where(ChatMessage.chat_id == chat_id)
                                        .order_by(ChatMessage.created_at.desc()).limit(9))).all()
                await s.commit()
            history = [{"role": m.role, "text": m.text} for m in reversed(hist) if m.id != message_id][-8:]

            if isinstance(command, ApplyUserAction):
                result = await self.orch.handle_action(str(chat_id), str(run_id), command.action)
                await self._close_open_clarifications(chat_id, f"выбрано в интерфейсе: {command.action.get('type')}")
            else:
                # one run never spins for ever (a hung model or source call): it ends with a clear answer, the checkpoints stay
                result = await asyncio.wait_for(self.orch.handle_message(str(chat_id), str(run_id), str(message_id), text, history), self.deadline)
            await self._finish(run_id, chat_id, message_id, result)
        except Exception as e:  # noqa: BLE001
            log.error("run %s failed: %s\n%s", run_id, e, traceback.format_exc())
            await self._fail(run_id, message_id, e)
        finally:
            hb.cancel()

    async def _close_open_clarifications(self, chat_id: uuid.UUID, answer: str) -> None:
        async with self.db.sessionmaker() as s:
            await s.execute(update(Clarification).where(Clarification.chat_id == chat_id, Clarification.status == "open")
                            .values(status="answered", answer=answer, answered_at=_now()))
            await s.commit()

    async def _finish(self, run_id: uuid.UUID, chat_id: uuid.UUID, message_id: uuid.UUID | None, result) -> None:
        turn: AgentTurn = result.turn
        plan_id = await self.store.plan_id(str(chat_id))
        async with self.db.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            status = "done"
            if result.waiting_user:
                # one batched question = one clarification: the user's next message answers all of it
                q = Clarification(chat_id=chat_id, run_id=run_id,
                                  field=",".join(x["field"] for x in turn.questions) or turn.kind,
                                  question=turn.text, options=_merged_options(turn))
                s.add(q)
                await s.flush()
                turn.questions = [{"id": str(q.id), "field": q.field, "text": q.question, "options": q.options or []}]
                await emit_event(s, chat_id, EventType.CLARIFICATION_REQUIRED, ClarificationRequired(
                    clarification_id=q.id, question=q.question, options=q.options, field=q.field), run_id)
                status = "waiting_clarification"
            reply = ChatMessage(chat_id=chat_id, role="assistant", text=turn.text, plan_id=plan_id,
                                plan_revision=turn.plan_revision or None, turn=turn.model_dump(mode="json"))
            s.add(reply)
            await s.flush()
            await emit_event(s, chat_id, EventType.ASSISTANT_MESSAGE, AssistantMessage(
                message_id=reply.id, text=reply.text, plan_id=plan_id, revision=turn.plan_revision or None), run_id)
            if message_id:
                (await s.get(ChatMessage, message_id)).status = "done"
            run.status, run.finished_at = status, _now()
            await s.commit()

    async def _fail(self, run_id: uuid.UUID, message_id: uuid.UUID | None, e: Exception) -> None:
        friendly = ("Сейчас не получается связаться с языковой моделью. Проверьте GIGACHAT_AUTH_KEY и попробуйте ещё раз."
                    if isinstance(e, LLMError) else
                    "Запрос занял слишком много времени, и я остановился. Уже найденное сохранено: повторите сообщение или попросите короче."
                    if isinstance(e, asyncio.TimeoutError) else "Не получилось обработать запрос. Попробуйте переформулировать или повторить.")
        async with self.db.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            run.status, run.error, run.finished_at = "failed", str(e)[:500], _now()
            if message_id and (msg := await s.get(ChatMessage, message_id)):
                msg.status = "failed"
            await emit_event(s, run.chat_id, EventType.REQUEST_FAILED, RequestFailed(reason=friendly), run_id)
            turn = AgentTurn(plan_revision=0, kind="error", text=friendly)
            reply = ChatMessage(chat_id=run.chat_id, role="assistant", text=friendly, turn=turn.model_dump(mode="json"))
            s.add(reply)
            await s.flush()
            await emit_event(s, run.chat_id, EventType.ASSISTANT_MESSAGE, AssistantMessage(message_id=reply.id, text=friendly), run_id)
            await s.commit()


def _merged_options(turn: AgentTurn) -> list[str] | None:
    opts = [o for q in turn.questions for o in (q.get("options") or [])] + list(turn.quick_replies or [])
    return list(dict.fromkeys(opts)) or None


async def main() -> None:
    from ..services import make_orchestrator
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings, db = get_settings(), get_database()
    store = DbPlanStore(db)
    worker = Worker(db, lambda: make_orchestrator(settings, store), store, settings.run_stale_seconds, settings.worker_poll_seconds, settings.run_deadline_seconds)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, worker.stop)
    await worker.loop()
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
