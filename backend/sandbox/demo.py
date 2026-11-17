"""The demo world of an offline stand (#53): the world's companies walked through the pipeline as an operator would.

Every step is a request to this very backend, the same the dashboard and the
sandbox's console send: no SQL and no service here, so the world always fits the
schema of the moment. The one thing written directly is the world's pages, where
the network would have put them.

The daemons keep ticking beside the scenario, so it waits for states, not for
outcomes: a row the background tick sent first is as sent. The fault switches are
the process's own, so a fault is on only while the one row it is for is due.
"""

import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import httpx

from sandbox import world

# Enough for a dozen statuses of the fake Node, each a second late.
WAIT_SECONDS = 30
POLL_SECONDS = 0.1
# Rounds of «move the clock, tick» before a state counts as never reached.
ROUNDS = 12
# Clock moves in seconds: past the longest jitter and past the longest backoff.
JITTER = {"preset": "jitter"}
PAST_BACKOFF = {"seconds": 30 * 60}
DAY = 86_400
# The funnel over every send of the world: the scenario moves the clock by about two weeks.
ANALYTICS_DAYS = 60
# The verdict table of the offline seller (docs/agents/workflow.md, «Ручной QA») and
# what each answer leaves the thread in; none of the words is a stop-word of delivery.
REPLIES: dict[world.Role, tuple[str, str]] = {
    "answer": ("Расскажите подробнее, что вы предлагаете?", "active"),
    "price": ("А сколько это стоит?", "escalated"),
    "interested": ("Давайте созвонимся завтра после обеда", "escalated"),
    "refusal": ("Спасибо, но мы отказываемся", "closed_refused"),
    "wrong": ("Вы ошиблись, мы таким не занимаемся", "closed_junk"),
    "junk": ("абракадабра", "closed_junk"),
    "stopword": ("Не пишите нам больше", "closed_refused"),
    "media": ("", "escalated"),
}
# Read, not only delivered: the run's lead and one more.
READ: tuple[world.Role, ...] = ("answer", "interested")
# Sent as the operator edited it: the free marking of the prompt on «Аналитика».
EDITED: tuple[world.Role, ...] = ("price",)
OPERATOR_EDIT = " Удобно созвониться на этой неделе?"
MEETINGS: dict[world.Role, str] = {"interested": "meeting_held", "price": "meeting_agreed"}


class DemoFailedError(Exception):
    """A step of the scenario got an answer it did not expect: the world is half-built."""


class Summary(TypedDict):
    companies: int
    leads: int
    threads: int
    queue: int
    run_id: str


def build(client: httpx.Client, pages: Path, now: datetime) -> Summary:
    """Lay the world's pages under `pages` and walk it through the pipeline over `client`.

    Dates count back from `now`: the same command builds the same world.

    Raises:
        DemoFailedError: an answer the scenario did not expect, or a state never reached.
    """
    world.store(pages, now)
    return _Operator(client).walk()


class _Operator:
    """What the operator presses, over HTTP; `ids` are the companies of the world's roles."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._ids: dict[str, str] = {}

    def walk(self) -> Summary:
        self._research()
        run = self._call(
            "POST", "/api/sandbox/runs", {"company_id": self._ids["answer"]}, expect=201
        )
        self._clock({"preset": "window"})
        firsts = [role for role in world.LEADS if role not in ("held", "draft", "draft_second")]
        for role in firsts:
            self._call("POST", f"/api/threads/{self._ids[role]}/draft", {"kind": "first"})
        for role in (*REPLIES, "touches"):
            self._send(role)
        # A lead answers what reached the phone; an answer before the receipt keeps it queued.
        self._wait(self._all_delivered, "статусы доставки")
        for role, (text, status) in REPLIES.items():
            self._reply(role, text, status)
        self._stuck("stuck")
        self._failed("failed")
        outbox_id = self._queue("cancelled")["outbox_id"]
        self._call("POST", f"/api/sender/queue/{outbox_id}/cancel")
        self._touches("touches")
        # Held by manual right after a send: the jitter keeps a background tick off it too.
        self._held("held")
        self._pool()
        for role, outcome in MEETINGS.items():
            self._call("POST", f"/api/threads/{self._ids[role]}/outcome", {"outcome": outcome})
        self._outreach()
        self._tick("monitor")
        self._wait(self._all_delivered, "статусы доставки")
        sourcing = self._call("GET", "/api/stats")["sourcing"]
        if sourcing is None:
            raise DemoFailedError("счётчики «Сбора лидов» не посчитались — см. backend.log")
        threads = [thread["company_id"] for thread in self._call("GET", "/api/threads")["threads"]]
        return Summary(
            companies=sourcing["companies"],
            leads=sourcing["leads"],
            threads=len(threads),
            queue=sum(len(self._queue_of(company_id)) for company_id in threads),
            run_id=run["run"]["run_id"],
        )

    # --- system 1 -------------------------------------------------------------------------

    def _research(self) -> None:
        segments = {"cities": [world.CITY], "rubrics": [*world.RUBRICS, world.FAILING_RUBRIC]}
        self._call("PUT", "/api/companies/segments", segments)
        self._call("POST", "/api/research/batches", expect=202)
        self._wait(
            lambda: (
                self._call("GET", "/api/research/batches/current")["status"]
                not in ("collecting", "researching")
            ),
            "партия исследования",
        )
        batch = self._call("GET", "/api/research/batches/current")
        if batch["status"] != "done":
            raise DemoFailedError(f"партия кончилась {batch['status']}: {batch['error']}")
        names = {
            lead["name"]: lead["company_id"] for lead in self._call("GET", "/api/research/leads")
        }
        self._ids = {firm.role: names[firm.name] for firm in world.FIRMS if firm.name in names}
        missing = set(world.LEADS) - self._ids.keys()
        if missing:
            raise DemoFailedError(f"research не сделал лидами: {sorted(missing)}")

    # --- the queue --------------------------------------------------------------------------

    def _queue(self, role: world.Role, text: str | None = None) -> dict[str, Any]:
        body = {"thread_id": self._ids[role]} | ({"text": text} if text else {})
        row: dict[str, Any] = self._call("POST", "/api/sender/queue", body, expect=201)
        return row

    def _send(self, role: world.Role) -> None:
        """Queue the role's draft as the operator and tick until it is sent."""
        text = None
        if role in EDITED:
            text = self._thread(role)["draft"]["draft_text"] + OPERATOR_EDIT
        if role in READ:
            self._faults({"delivery": "read"})
        try:
            outbox_id = self._queue(role, text)["outbox_id"]
            self._until(lambda: self._row(role, outbox_id) == "sent", self._next_send, role)
        finally:
            self._faults({"delivery": "delivered"})

    def _next_send(self) -> None:
        """Into the window and past the jitter, then one tick."""
        self._clock({"preset": "window"})
        self._clock(JITTER)
        self._tick()

    def _stuck(self, role: world.Role) -> None:
        """The transport answers with nothing known: the tick hands the row to a human."""
        outbox_id = self._queue(role)["outbox_id"]
        self._faults({"send": "unknown"})
        try:
            self._until(lambda: self._row(role, outbox_id) == "stuck", self._next_send, role)
        finally:
            self._faults({"send": "ok"})

    def _failed(self, role: world.Role) -> None:
        """The frame never leaves: retried with backoff until the row fails."""
        outbox_id = self._queue(role)["outbox_id"]
        self._faults({"send": "not_sent"})
        try:
            self._until(lambda: self._row(role, outbox_id) == "failed", self._next_retry, role)
        finally:
            self._faults({"send": "ok"})

    def _next_retry(self) -> None:
        """Into the window and past the longest backoff, then one tick."""
        self._clock({"preset": "window"})
        self._clock(PAST_BACKOFF)
        self._tick()

    def _held(self, role: world.Role) -> None:
        """The automaton queues a first touch in full, and manual holds its row."""
        self._call("POST", f"/api/threads/{self._ids[role]}/draft", {"kind": "first"})
        # The automaton takes the first draft of a thread not delivered yet: only this one is left.
        self._wait(self._all_delivered, "статусы доставки")
        self._autopilot("full")
        try:
            self._until(lambda: bool(self._rows_of(role)), self._tick, role)
        finally:
            self._autopilot("manual")

    # --- the lead -------------------------------------------------------------------------

    def _reply(self, role: world.Role, text: str, status: str) -> None:
        """Answer as the lead through the fake Node and tick until the thread settles."""
        self._call("POST", "/api/sandbox/incoming", {"company_id": self._ids[role], "text": text})

        def settled() -> bool:
            thread = self._thread(role)
            # An answer of the seller in manual stays a draft for the operator.
            return thread["status"] == status and (status != "active" or thread["draft"])

        self._until(settled, self._tick, role)

    def _touches(self, role: world.Role) -> None:
        """The lead stays silent: each pause brings a follow-up, the last one exhausts the thread."""
        for days in self._call("GET", "/api/sender/settings")["follow_up_days"]:
            self._clock({"seconds": days * DAY})
            self._until(lambda: self._thread(role)["draft"] is not None, self._tick, role)
            self._send(role)
        if self._thread(role)["status"] != "exhausted":
            raise DemoFailedError(f"{role}: касания кончились, а тред не исчерпан")

    # --- the pool and the drafts --------------------------------------------------------------

    def _pool(self) -> None:
        """A second number warms up, a third is banned; the warmup writes to the second."""
        warming, banned = world.POOL
        for number in world.POOL:
            self._call("POST", "/api/sender/numbers", {"number": number}, expect=201)
        ban = {"status": "banned", "note": "демо: номер забанен"}
        self._call("POST", f"/api/sender/numbers/{banned}/status", ban)
        # A day past the socket delay: the warmup calendar moves the number on.
        self._clock({"preset": "day"})
        self._until(
            lambda: self._number(warming) == "warming" and self._warmup_log(),
            lambda: self._tick("warmup"),
            "прогрев",
        )

    def _outreach(self) -> None:
        """«Черновики топ-N»: the leads left without a thread get their first drafts."""
        self._call("POST", "/api/threads/outreach", expect=202)
        self._wait(
            lambda: self._call("GET", "/api/threads/outreach/current")["status"] != "running",
            "черновики топ-N",
        )
        drafts: tuple[world.Role, ...] = ("draft", "draft_second")
        for role in drafts:
            if self._thread(role)["draft"] is None:
                raise DemoFailedError(f"{role}: «Черновики топ-N» не написали черновик")

    # --- reading --------------------------------------------------------------------------

    def _thread(self, role: world.Role) -> dict[str, Any]:
        thread: dict[str, Any] = self._call("GET", f"/api/threads/{self._ids[role]}")
        return thread

    def _queue_of(self, company_id: str) -> list[dict[str, Any]]:
        """Return the thread's rows once each: a waiting row is among the latest too."""
        view = self._call("GET", "/api/sender/queue", params={"thread_id": company_id})
        return list({row["outbox_id"]: row for row in [*view["queue"], *view["recent"]]}.values())

    def _rows_of(self, role: world.Role) -> list[dict[str, Any]]:
        return self._queue_of(self._ids[role])

    def _row(self, role: world.Role, outbox_id: int) -> str | None:
        rows = self._rows_of(role)
        return next((row["status"] for row in rows if row["outbox_id"] == outbox_id), None)

    def _number(self, number: str) -> str | None:
        cards = self._call("GET", "/api/sender")["numbers"]
        return next((card["status"] for card in cards if card["number"] == number), None)

    def _warmup_log(self) -> bool:
        return bool(self._call("GET", "/api/sender")["warmup_log"])

    def _all_delivered(self) -> bool:
        """Return whether every thread sent to has a receipt; the funnel is what counts both."""
        report = self._call("GET", "/api/analytics", params={"days": str(ANALYTICS_DAYS)})
        steps = {step["step"]: step["count"] for step in report["funnel"]}
        return bool(steps["delivered"] == steps["sent"])

    # --- the console --------------------------------------------------------------------------

    def _tick(self, daemon: str = "tick") -> None:
        self._call("POST", "/api/sandbox/tick", {"daemon": daemon})

    def _clock(self, move: dict[str, Any]) -> None:
        self._call("POST", "/api/sandbox/clock", move)

    def _faults(self, switches: dict[str, str]) -> None:
        self._call("POST", "/api/sandbox/faults", switches)

    def _autopilot(self, mode: str) -> None:
        values = self._call("GET", "/api/sender/settings") | {"autopilot": mode}
        self._call("PUT", "/api/sender/settings", values)

    # --- the plumbing ---------------------------------------------------------------------------

    def _call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        expect: int = 200,
        params: dict[str, str] | None = None,
    ) -> Any:
        response = self._client.request(method, path, json=body, params=params)
        if response.status_code != expect:
            raise DemoFailedError(
                f"{method} {path}: {response.status_code} вместо {expect}: {response.text[:300]}"
            )
        return response.json()

    def _until(self, done: Callable[[], bool], step: Callable[[], None], what: str) -> None:
        """Take `step` until `done`: the state, not the step's outcome, says when."""
        for _ in range(ROUNDS):
            if done():
                return
            step()
        if not done():
            raise DemoFailedError(f"{what}: состояние не наступило за {ROUNDS} шагов")

    def _wait(self, done: Callable[[], bool], what: str) -> None:
        """Wait for background work of this backend: a batch, a run, the fake Node's statuses."""
        deadline = time.monotonic() + WAIT_SECONDS
        while not done():
            if time.monotonic() > deadline:
                raise DemoFailedError(f"{what}: не дождались за {WAIT_SECONDS} с")
            time.sleep(POLL_SECONDS)
