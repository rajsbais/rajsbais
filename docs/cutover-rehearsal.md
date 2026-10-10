# Cutover rehearsal checklist

A cutover rehearsal is one pass through the cutover of a manifest before the real one: a **mock cutover**, a
**dress rehearsal**, or the **go-live checklist** itself (`kind` MOCK / DRESS / FINAL). The platform tracks each
pass as a checklist, times the runbook tasks, collects lessons and records the approver's verdict. It never
executes a cutover step itself: SAP-side actions (freezes, interface stops, backups, number ranges) are done by
people and recorded by hand; the platform contributes what it can verify from its own state.

Where to find it: **Cutover Command Center** page (below the runbook), `POST /manifests/{id}/cutover/rehearsals`
and `/cutover/rehearsals/{id}/...`, `sdtf cutover-rehearsal`.

## The checklist

Every rehearsal starts from the same catalogue (`GET /cutover/checklist-template`). Each item has a phase, an
owner, the runbook task it belongs to, and a **blocking** flag: GO is refused while a blocking item is neither
PASS nor NOT_APPLICABLE. Non-blocking items are recorded, listed in the report, and left to the approver's
judgement.

**Automatic items** are evaluated from the platform's state when the rehearsal is created, on every *refresh*,
and once more when the verdict is given, so a GO never rests on stale state:

| # | Item | Blocking | What is checked |
|---|---|---|---|
| A01 | Scope manifest approved | yes | manifest status APPROVED and the approver |
| A02 | No pending business dispositions | yes | objects still flagged `requires_approval` |
| A03 | Transformation ruleset approved | yes | the ruleset of the latest completed run |
| A04 | Completed run on the manifest | yes | latest completed (non-delta) run, its mode and load mode |
| A05 | Reconciliation PASS, or WARN with every variance explained | yes | overall result and non-PASS checks without explanation |
| A06 | No open ERROR exceptions on the latest run | no | exceptions with disposition OPEN |
| A07 | Technical and business sign-off recorded | yes | approval records on the run's reconciliation |
| A08 | Source freeze declared and final delta reconciled PASS | yes | delta state of the baseline run; **NOT_APPLICABLE** when the source connector cannot capture changes |
| A09 | Cockpit packages simulated and converged | yes | package rounds of the run; **NOT_APPLICABLE** when nothing routed to the cockpit; FAIL while no package is exported or a round is not simulated |
| A10 | Evidence package written | no | evidence files in the run report |
| A11 | Cutover risk assessed and not HIGH | no | latest `cutover_risk` decision on the manifest |
| A12 | Carve-out completeness | no | completeness report of the manifest |
| A13 | Target system registered with its connector | no | target of the manifest, connector and connector status |

An automatic item cannot be ticked by hand. It can be **waived** (NOT_APPLICABLE with a mandatory note, recorded
as a waiver and shown as such) and un-waived; a waived item keeps its waiver across refreshes.

**Manual items** are what the platform cannot see. They are ticked PASS, FAIL (note required), NOT_APPLICABLE or
reset to PENDING, and every tick is an audit event with the actor:

| # | Item | Blocking | Task |
|---|---|---|---|
| M01 | Change freeze and transport lock confirmed in source and target | yes | T01 |
| M02 | Interface inventory reviewed: stop / switch plan with an owner for every interface | yes | T06 |
| M03 | Batch jobs and background processing stop list agreed and scheduled | yes | T06 |
| M04 | Backups / restore points taken on source and target before the window | yes | T06 |
| M05 | Target number ranges checked (external numbering for carried-over documents, buffers reset) | yes | T10 |
| M06 | Users locked for the window; emergency and go-live users prepared | yes | T06 |
| M07 | Communication plan shared (window, war room, escalation contacts) | no | T02 |
| M08 | Rollback rehearsed before the point of no return | yes | T10 |
| M09 | Hypercare roster and business validation scripts ready | no | T12 |
| M10 | Residual data disposition in the source approved by legal | no | T11 |
| M11 | Interface switch-over executed on the rehearsal landscape and verified | no | T10 |

## Lifecycle

```
PLANNED --start--> IN_PROGRESS --complete GO | NO_GO--> COMPLETED
   |                    |
   +------ abort -------+-----------------------------> ABORTED
```

* **Create** (`run:start`): snapshot of the runbook (tasks with their estimates), checklist with the automatic
  items evaluated. A manifest can have any number of rehearsals; they are numbered.
* **Start** (`run:start`): from then on runbook tasks can be timed.
* **Time a task** (`run:start`): `start` and `finish` per runbook task; the actual minutes are kept with who
  recorded them and a note. A task can be restarted after it finished (the last measurement counts).
* **Lessons** (`run:start`): free text, optionally tied to a task.
* **Complete** (`approve:run`): the approver's verdict. GO re-evaluates the automatic items and is refused while a
  blocking item is open; NO_GO is always accepted. The verdict is also written as an approval record
  (`REHEARSAL`, APPROVED for GO / REJECTED for NO_GO). A completed rehearsal's checklist is frozen.
* **Abort** (`run:start`): the rehearsal is closed without a verdict.

Agents never create, tick or complete a rehearsal (go/no-go stays a human decision, `docs/08-security-approval-model.md`).

## What feeds back

* **Runbook forecast**: for every task a completed rehearsal timed (GO or NO_GO alike; an aborted rehearsal
  contributes nothing), the runbook uses the measured minutes instead of the template estimate; each task now
  carries its `basis` (`template`, `simulated run x20`, `rehearsal N`) and the runbook lists
  `rehearsal_timed_tasks`. The forecast is still not a measured downtime guarantee: it mixes measured and
  template values and the rehearsal landscape is not production.
* **Cutover risk agent**: `rehearsals_completed`, `rehearsals_go`, `latest_rehearsal`, `blocking_items_open` and
  `interface_plan` (item M02 of the latest rehearsal) are factors; the go/no-go criteria "at least one completed
  rehearsal with verdict GO" and "interface cut-over plan reviewed" read the rehearsals instead of counting
  completed runs. Completed runs are reported separately (`completed_runs`).
* **Report**: `GET /cutover/rehearsals/{id}/report` and `sdtf cutover-rehearsal report` render the checklist,
  timings and lessons as Markdown for the cutover binder / evidence package.

## Live execution

Once a rehearsal (or the go-live checklist) is started, `GET /cutover/rehearsals/{id}/timeline` and
`sdtf cutover-rehearsal timeline` show the runbook against the clock (`backend/sdtf/cutover/execution.py`):

* **Planned window** per task from the start of the rehearsal and the dependency graph (earliest start and finish
  from the estimates the runbook snapshot carries).
* **Actual**: a task timed by hand, or, where the platform did the work itself, **observed from its own runs**: the
  initial extraction / transformation / load (T04) from the stages of the completed run, the delta cycles (T05),
  the final delta (T07) and its reconciliation stage (T08, or the baseline run's reconciliation when the source
  cannot capture changes). A hand timing wins; an observation from before the rehearsal started is shown as
  *history* on the task, never as the task done in this rehearsal.
* **Status** per task: DONE, RUNNING, READY (dependencies done), WAITING; **late minutes** against the planned
  window; a **projection** of the rest from what has happened (running tasks finish no earlier than now, the rest
  follow their dependencies), hence a projected end and projected total.
* **Downtime clock**: from the business freeze (the first downtime task) started in this rehearsal to the last
  downtime task finished; planned, elapsed and projected minutes.
* **Incidents** (`POST .../incidents`, `.../incidents/{id}/escalate`, `.../incidents/{id}/resolve`; `run:start`):
  raised on a runbook task with a severity (LOW, MEDIUM, HIGH, CRITICAL), a title and a detail. The escalation
  path follows the task's owner (for example Basis: Basis lead → IT operations manager → Cutover manager →
  Steering committee); *escalate* moves one level up or to a named person or role, and a CRITICAL incident is
  escalated to the first level the moment it is raised. A HIGH or CRITICAL incident that is still open **refuses
  GO**, like a blocking checklist item. Resolution needs a note. Every step is an audit event.
* **Assignments** (`PUT .../assignments/{task}`, `GET .../assignments`): who runs each task during this rehearsal,
  a backup and how to reach them (a name or role and a channel, never a credential). The summary lists the downtime
  tasks nobody is assigned to; the report carries the assignments and the incidents.

Nothing here reaches an SAP system or pages anyone: the platform records what people report and what it ran
itself; the escalation is a recorded step with a named level, the call is made by people.

## CLI

```bash
sdtf cutover-rehearsal create --manifest <id> --name "Mock cutover 1" --kind MOCK
sdtf cutover-rehearsal show --id <rehearsal>            # checklist, timings, lessons
sdtf cutover-rehearsal mark --id <rehearsal> --item M01 --status PASS --note "transport lock set"
sdtf cutover-rehearsal start --id <rehearsal>
sdtf cutover-rehearsal task --id <rehearsal> --task T06 --action start
sdtf cutover-rehearsal task --id <rehearsal> --task T06 --action finish --note "interfaces stopped in 3 min"
sdtf cutover-rehearsal lesson --id <rehearsal> --text "stop the IDoc inbound queue first" --task T06
sdtf cutover-rehearsal refresh --id <rehearsal>         # re-evaluate the automatic items
sdtf cutover-rehearsal complete --id <rehearsal> --verdict GO --note "go for the dress rehearsal"
sdtf cutover-rehearsal report --id <rehearsal> --out rehearsal-1.md
sdtf cutover-rehearsal timeline --id <rehearsal>        # live execution: tasks against the clock, downtime, incidents
sdtf cutover-rehearsal assign --id <rehearsal> --task T06 --assignee "Basis on-call" --backup "Integration lead" --contact "war room bridge"
sdtf cutover-rehearsal incident --id <rehearsal> --action raise --task T07 --severity CRITICAL --title "final delta failed"
sdtf cutover-rehearsal incident --id <rehearsal> --action escalate --incident I01-abc123 --note "no root cause after 20 min"
sdtf cutover-rehearsal incident --id <rehearsal> --action resolve --incident I01-abc123 --note "delta replayed"
```

## Honesty notes

* The automatic items describe the platform's own records: on this build that is the simulated runtime
  (synthetic landscape, simulated gateway, simulated add-on). A PASS on A04/A05/A08/A09 says the simulated run,
  reconciliation, final delta and cockpit rounds passed, not that a real system did.
* The manual items are statements people recorded; the platform cannot verify a transport lock or a backup.
* Live execution tracking records what people report and what the platform ran itself; "fed by the systems"
  means the platform's own run stages. It does not read task status from an SAP system, a scheduler or a
  ticketing tool, and it does not page anyone.
