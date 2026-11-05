"""STUB for the agents module, for local runs and demos (DEV_AGENT=true). Not part of the product.

It plays the agents' side of the contract so the whole path can be exercised without GigaChat and MCP:
it takes `queued` runs one chat at a time, publishes progress, asks one clarification when the request has no
digits (no party size / dates), then writes the mock plan from trip_map/examples to trip_plans and answers.
"""
import asyncio
import logging
import re

from sqlalchemy import select

from shared.commands import ProcessClarificationAnswer, agent_command_adapter
from shared.database import Database
from shared.events import (AgentProgress, AssistantMessage, ClarificationRequired, EventType, PlanChanged,
                           RequestFailed, emit_event)
from shared.models import AgentRun, ChatMessage, Clarification

from .mock_plans import EXAMPLES, pick_mock, save_mock_plan  # noqa: F401  (re-exported for tests)
from .repository import latest_plan, utcnow

log = logging.getLogger(__name__)

class DevAgent:
    def __init__(self, db: Database, step_delay: float = 0.4):
        self.db, self.step_delay = db, step_delay

    async def run(self) -> None:
        while True:
            try:
                if not await self.step():
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dev agent step failed")
                await asyncio.sleep(1)

    async def step(self) -> bool:
        """Process one queued run, if any. One run per chat at a time, in creation order."""
        async with self.db.sessionmaker() as s:
            busy = set(await s.scalars(select(AgentRun.chat_id).where(AgentRun.status == "running")))
            queued = await s.scalars(select(AgentRun).where(AgentRun.status == "queued").order_by(AgentRun.created_at))
            run = next((r for r in queued if r.chat_id not in busy), None)
            if run is None:
                return False
            run.status, run.started_at = "running", utcnow()
            run_id = run.id
            await s.commit()
        try:
            await self._process(run_id)
        except Exception as e:
            log.exception("run %s failed", run_id)
            async with self.db.sessionmaker() as s:
                run = await s.get(AgentRun, run_id)
                run.status, run.error, run.finished_at = "failed", str(e), utcnow()
                await emit_event(s, run.chat_id, EventType.REQUEST_FAILED,
                                 RequestFailed(reason="Не удалось обработать запрос."), run.id)
                await s.commit()
        return True

    async def _progress(self, run: AgentRun, text: str) -> None:
        async with self.db.sessionmaker() as s:
            await emit_event(s, run.chat_id, EventType.AGENT_PROGRESS, AgentProgress(text=text), run.id)
            await s.commit()
        await asyncio.sleep(self.step_delay)

    async def _process(self, run_id) -> None:
        async with self.db.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            command = agent_command_adapter.validate_python(run.command)
            message = await s.get(ChatMessage, command.message_id)
            has_plan = await latest_plan(s, run.chat_id) is not None
            text = message.text
        await self._progress(run, "Разбираю запрос…")

        needs_details = (not has_plan and not isinstance(command, ProcessClarificationAnswer)
                         and not re.search(r"\d", text))
        async with self.db.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            message = await s.get(ChatMessage, command.message_id)
            message.status = "done"
            if needs_details:
                q = Clarification(chat_id=run.chat_id, run_id=run.id, field="party.adults",
                                  question="Сколько человек поедет и на какие даты?",
                                  options=["Двое, на выходные", "Один, на неделю", "Семья из четырёх"])
                s.add(q)
                await s.flush()
                run.status, run.finished_at = "waiting_clarification", utcnow()
                await emit_event(s, run.chat_id, EventType.CLARIFICATION_REQUIRED, ClarificationRequired(
                    clarification_id=q.id, question=q.question, options=q.options, field=q.field), run.id)
                await s.commit()
                return
        await self._progress(run, "Ищу проживание и переезды…")

        async with self.db.sessionmaker() as s:
            run = await s.get(AgentRun, run_id)
            # a clarification-answer run supersedes the run that was waiting for it
            for old in await s.scalars(select(AgentRun).where(
                    AgentRun.chat_id == run.chat_id, AgentRun.status == "waiting_clarification")):
                old.status = "done"
            texts = list(await s.scalars(select(ChatMessage.text).where(
                ChatMessage.chat_id == run.chat_id, ChatMessage.role == "user").order_by(ChatMessage.created_at)))
            mock = pick_mock(texts)  # the whole conversation: a clarification answer alone says nothing about the city
            plan = await save_mock_plan(s, run.chat_id, mock)
            changed = PlanChanged(plan_id=plan.id, revision=plan.revision, changed_node_ids=list(mock["nodes"]))
            await emit_event(s, run.chat_id, EventType.PLAN_UPDATED, changed, run.id)
            await emit_event(s, run.chat_id, EventType.PLAN_READY, changed, run.id)
            reply = ChatMessage(chat_id=run.chat_id, role="assistant", plan_id=plan.id, plan_revision=plan.revision,
                                text=f"Собрал поездку «{mock['title']}» (демо-данные): отели и переезды. Откройте карту.")
            s.add(reply)
            await s.flush()
            await emit_event(s, run.chat_id, EventType.ASSISTANT_MESSAGE, AssistantMessage(
                message_id=reply.id, text=reply.text, plan_id=plan.id, revision=plan.revision), run.id)
            run.status, run.finished_at = "done", utcnow()
            await s.commit()
