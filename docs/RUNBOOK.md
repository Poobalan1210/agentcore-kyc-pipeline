# Runbook — KYC in 90 Seconds

Everything needed to run the talk's live demo: what must be true beforehand, the three acts, how
to reset between runs, what to do when something goes wrong on stage, and the operations that
are not part of the show (the Cedar switch, re-driving Phase 2, removing the demo controls).

Nothing in this file has been run against a deployment from the `reinvent-ready` branch. The
acts are checked offline (`make evals` runs every persona through the real tools and rules), and
section 9 lists what only a live rehearsal can confirm.

---

## 1. What must be true before the day

| Item | How | Who |
|---|---|---|
| Written consent from each presenter whose face is captured, naming AWS retention (D-14) | Signed, kept with the consent records | **People** — the build cannot do this |
| Disclosure slide with D-14's wording | In the deck | People |
| Presenter photos on their personas' documents | `demo-assets/photos/demo-us-0N.jpg`, then `make demo-assets` | Presenters |
| Each persona's ID printed, to hold up to the webcam | `demo-assets/generated/demo-us-0N-passport.pdf`, at 100% scale | Presenters |
| Stacks deployed, and the Cedar mode chosen: ENFORCE once section 5 passes, or LOG_ONLY, which Backstage labels *logging, not blocking* (a bare LOG_ONLY read as "the control is off") | `make deploy` (section 5 for ENFORCE) | Operator |
| Presenter and reviewer accounts in their groups | `scripts/prepare_demo.py users ... --apply` | Operator |
| The console's env file | section 6 of the README | Operator |
| A full rehearsal, every act, recorded as a backup video | Section 3, end to end | Presenters |
| Rehearsal measurements in `docs/MEASUREMENTS.md` | `make measure`, `make export` | Operator |

Which persona each presenter runs, and therefore whose photo goes on which document:

| Persona | Act | Face on the document |
|---|---|---|
| DEMO-US-03 Nadia Rose Korval | 1 and 3 | Presenter A |
| DEMO-US-02 Avery Quinn Hale | 2 | Presenter B |
| DEMO-US-04 Dmitri Volkov | 2 | Presenter A or B |
| DEMO-US-01 Taylor Jordan Reeves | spare clean path | either |

The face doing the liveness check must be the face on the document, or the face match fails and
the case goes to a reviewer (which the evals confirm, and which is correct).

---

## 2. Before each run (and between rehearsals)

1. **Reset the demo controls** — console → Demo controls → Reset, or
   `scripts/prepare_demo.py reset --apply`.
   This is not optional. A list update left published from the last run means Nadia's Phase 1
   screening runs against the updated list: she is a CONFIRMED match at onboarding and is
   declined, and Act 3 has no account to freeze. Reset also clears the device and network counts
   the Fraud check reads (D-41), and leaves every case alone. The counts are of different people,
   so restarting a persona never adds to them; the run of show's four personas stay at four, under
   the five that would send a case to a person. Other names tried from the same browser do count
   until the next Reset.
2. **Decide any open reviews** left from the last run, so the queue on screen holds only today's.
3. `make preflight` — every stack complete, controls clear, both groups populated, and where each
   persona stands. Exit 0 means ready.
4. `make evals` — 45 of 45. A failure names the act that would fail.
5. Start a **new application** for every persona each run (Live demo → *New application* → the
   persona, or "Start a new application" on the device, which opens the same persona again): a
   case decides once, by design.

Screen: one console, http://localhost:5173 (`cd frontend && npm run dev`), on the shared screen.
Live demo puts the applicant's phone on the left and the case's whole process on the right; the
top bar switches to the Review queue, All cases and Demo controls without losing the applications
in progress. The phone shows only the bank's app (AnyCompany Bank): *New application* is where
the persona is chosen, its details arrive filled in, and the note under the phone names the
persona and its ID file. Each application keeps its own tab (Nadia, Avery, Dmitri), and the tabs
survive a reload. One presenter account in both `presenters` and `reviewers` runs every act; with
two presenters, open the console on both laptops and sign the second in as the reviewer.

The process side has two views of the same records, switched in its header (D-38). **Backstage**
(the default) is the case as it runs: each phase as a timeline on its own clock, one lane per
service, the four agents' bars side by side, each tool's fact as a dot where it landed, and a
live feed of every write underneath. **Story** is the checklist, in plain words. *Step Functions
console* on a phase opens that execution in the AWS console, for anyone who asks whether it is
real. Backstage reads the execution history, so it needs the reviewer account; anyone else gets
Story.

Each agent lane (D-40) is labelled *AgentCore harness*. Its dot is where its tool's fact landed:
green if the check passed, grey if it could not be established, red if it did not run.
*Recorded* is that fact. *Said* is the agent's own words, which the rules never read. One line
above Phase 1's lanes says this and shows the Cedar mode. Clicking a lane opens its drawer: the
harness and its version, the Gateway and the Cedar policy engine (once the case has a decision
record), the model and the tokens. Open it once on stage, for one agent, and leave the rest
closed. **Large text** in the top bar makes the process side and the review console larger for
the room and leaves the phone alone. Use it instead of browser zoom. With it on, Phase 1 is taller
than the screen: scroll so the lanes you are talking about are in view.

---

## 3. Run of show

### Act 1 — ninety seconds (Nadia, DEMO-US-03)

The phone runs the bank's onboarding in the order a real one does (D-37): consent, details, ID,
selfie, review, submit. Nothing reaches the bank until Submit.

1. Live demo → *New application* → **Nadia Rose Korval** (under *Act 1, then Act 3*). The phone
   opens on AnyCompany Bank's welcome screen, United States and a checking account chosen. Tick
   the consent (*How we use your face* opens the notice) → *Get started*.
2. *About you* arrives filled in → *Continue*.
3. *Add your ID*: Passport, issued by the United States → **Take a photo**. The camera takes the
   window: hold the printed passport page up to the webcam inside the frame, flat and still, then
   *Take photo* (or Space) → *Use this photo* → *Continue*. Only the inside of the frame is kept,
   so the presenter's own face never ends up in the document photo. If the camera will not do:
   *Upload a photo instead* → `demo-us-03-passport.jpg` (the note under the phone names it).
4. *Selfie video*: the face check takes the whole window, with the oval right under the webcam.
   Look straight into the camera, move closer until the face fills the oval, hold still. There is
   nothing to press and, on a demo deployment, nothing flashes (D-36).
5. *Check and submit* lists everything → **Submit application**. This is when the four agents
   start. On Backstage: the submit flag at zero, the queue hand-over, then four agent bars running
   side by side in one Parallel state, a dot as each tool records its fact, then the rules, the
   sealed record and the account, with the timer against the 90-second target. Point at the
   bracket: four agents at once, not one after another. Say the legend line once: each agent is an
   AgentCore harness that can reach its tool only through the Gateway; the tool writes the fact
   (the dot); the agent writes its reply (the faint end of the bar); the rules read only the fact.
   Fraud's tool counted how many different people applied from this laptop and this network in
   the last 24 hours (*Within limits · 1 person from this device in 24 h* for the first
   application after Reset); five from one device would send the case to a person. The feed under
   it lists every write as it lands.
6. The phone says *Verifying your identity*, with no counter: how long it takes is the bank's
   figure, on the right-hand side, from the case record's own timestamps. The board shows the
   same number.
7. Provisional decision: the phone opens the account (*Your account is open*, the balance, what
   is limited for now). Show the limits are real: tap **Send abroad** ($250) → *Not available
   yet: International transfers are not available yet.* The account platform refused it, not the
   app. *Add money* ($100) goes through.
8. Say what decided: four agents gathered the evidence through tools; each tool recorded a fact;
   the rules read the facts. Leave Phase 2 running — it lifts the limits a few minutes later, and
   the account updates by itself (*Your account is fully open*). Act 3 needs this account open;
   Phase 3 works on a provisional account too, so an unfinished Phase 2 does not block it.

### Act 2 — a person decides, and a namesake does not need one

1. Presenter B: *New application* → **Avery Quinn Hale** (DEMO-US-02), through the same flow
   with her printed passport. Screening finds a POSSIBLE match: SYN-0106 "Avery Hale" is listed
   with no date of birth, so the name can be neither confirmed nor excluded. The applicant sees
   *We're reviewing your application* — never why.
2. Review queue (or *Review now* on the stage that waits for a person): the case is in the queue
   with its reason. Open it: the briefing (written by
   the Briefing agent from the facts, labelled as such), the alert, and *Approving clears
   SYN-0106 (POSSIBLE)*. Show the Recorded facts tab, then the Agent narrative tab: the facts
   decided, the words explain.
3. Approve with a rationale. Avery's screen moves to *Your account is open*. The clearance is
   written with the decision and sealed in the audit record; no later screening raises SYN-0106
   for Avery again — Act 3 will show it.
4. *New application* → **Dmitri Volkov** (DEMO-US-04): the exact name of SYN-0001, born 26
   years later. Approved without a person, in the same ninety seconds. On the board, click his
   row: the Recorded facts tab shows the exclusion and its reason — two known dates of birth that
   differ.

### Act 3 — the list changes after the account opened

1. Demo controls → *Act 3 · Sanctions list update*: the update holds four new synthetic listings,
   as a real one arrives in a batch: SYN-0901 Nadia Rose Korval (1987-02-11, US) and three that
   name no customer → *Publish list update* → confirm (*re-screens every open account against 4
   new listings*). It reports how many open accounts were sent for re-screening. Then Live demo →
   Nadia's tab: her phone and her process side by side.
2. Phase 3 re-screens each account against the whole update (on Backstage, Phase 3 stops
   *Watching* and fills: the event, the re-screen, the rules, the decision history row, the
   sealed record, the freeze). Nadia: CONFIRMED on SYN-0901. Her
   account is **frozen immediately** — a sanctions breach is strict liability, so the freeze does
   not wait for a person — and queued for a reviewer to confirm. On her phone: *Your account is
   temporarily unavailable*, and every action is locked; *Send abroad* is refused (*The account is
   on hold*). Nothing on her screen says sanctions.
3. Everyone else is unchanged — Avery included, because her reviewer cleared SYN-0106 in Act 2,
   and the other three listings match no one. The board shows it.
4. Review queue: Nadia's case, reason *Frozen by the rules*, the CONFIRMED alert on
   SYN-0901 at the top. **Approve is disabled**: a confirmed match cannot be approved, and the
   review handler would refuse it anyway. *Exit · freeze* with a rationale.

### Optional — the failure this build is shaped around

Demo controls → Faults → *Sanctions screening* unavailable → Apply. Run *New application* →
Taylor Jordan Reeves (DEMO-US-01). The Pre-Screen
agent may well say "no match was found"; the screening fact records `performed: false`, and the
rules send the case to a person. On Backstage it is one lane: a red dot, *Recorded: Not
performed · UNAVAILABLE*, the agent's sentence quoted whole under *Said*, then *The rules read
the fact above, never these words* and, two lanes down, *Decision engine: Refer to a person*.
Point at those three; that is the whole point, in about fifteen seconds. **Reset afterwards.**

### Questions the screen raises

- *If the rules never read the reply, why wait for it?* The pipeline does wait: the rules start
  once all four agents have answered, so the faint end of each bar (the reply, after the fact
  landed) is on the clock. The reply is not wasted, though: the reviewer reads it, in the review
  console's agent narrative, beside the facts.
- *Can I see the agent's steps inside the harness?* Not on this screen. The harness emits no agent
  or tool spans (D-38), so the split in a bar comes from two timestamps, not from a trace.
  AgentCore's own trace view in CloudWatch is the place to look, if it shows them (section 9).
- *Is Cedar blocking anything?* Under LOG_ONLY, no: every call to a Gateway tool is evaluated and
  logged, and none is refused. Under ENFORCE a call the policies do not permit is refused before
  the tool runs (section 5). Either way, each harness exposes only its own allowed tools.
- *Isn't one laptop opening accounts for three people suspicious?* Yes, and that is what the
  Fraud count measures: five different people from one device in a day sends the case to a
  person. The run of show stays under it, and Reset clears the counts before each run. What is
  stored is a keyed hash of the browser's random id and of the network, never the IP address.
- *Where did the reviewer's rationale go?* Into the sealed decision record with the decision, not
  into any log or execution history. To show it: All cases → the case's row (or Review queue →
  *Open any case*) → *Decisions* → *Latest sealed audit record*.

---

## 4. Fallbacks on stage

| What you see | Why | What to do |
|---|---|---|
| Liveness stuck on *Connecting…* | The detector's camera init can wedge on some cameras | After 25 s the page offers *Start a new attempt*; close other apps using the camera. Fallback: continue with a persona onboarded at rehearsal |
| *This application has already been submitted* on the face check | A face check for a submitted case is refused (D-26) | *Back to my application*; or start a new application |
| The face check keeps saying *Center your face in the oval* | The face is turned away from the webcam, or the window is on a second display | Look into the lens, not at the oval; keep the console on the laptop screen whose camera is in use. *Cancel*, then *Try again* gets a fresh session |
| *Backstage* is greyed out | The signed-in account is not in `reviewers`, so the execution history cannot be read | Sign in with the reviewer account; *Story* shows the same case meanwhile |
| A Phase 1 timeline says its history is not available | The case ran before the runs named themselves on the case (D-38), or Step Functions has aged it out (90 days) | Start a new application for the persona; the facts are all on *Story* |
| The ID camera says it couldn't open the camera | Another app, or another tab, holds the webcam | Close it and *Take a photo* again, or *Upload a photo instead* with the persona's JPEG |
| A case whose ID was photographed goes to a reviewer (*We're reviewing your application*), its process showing the document not read | Glare, blur or a page held too far away: the MRZ did not read, and an unreadable document is a person's call, not a retry | Decide it in the Review queue (it failed closed, which is the property), or start a new application for the persona and upload its JPEG. Rehearse the camera under the stage lighting (section 9) |
| Colours flash, or a *Start the face check* button appears | The deployment runs the light challenge (`LIVENESS_CHALLENGE=FaceMovementAndLightChallenge`) | Expected for that setting. For the demo: `make deploy` with demo controls on, or redeploy `KycApi` with `-c livenessChallenge=FaceMovementChallenge` |
| A case goes to review unexpectedly (*A check could not run*) | An agent timed out or returned nothing, or a tool was down | It failed closed: show it in the console, approve with a rationale. That is the property, not a bug |
| Fraud says *Velocity critical* and the case goes to a reviewer | Five or more different people applied from this browser (or ten from this network) since the last Reset, rehearsal names included | Decide it in the Review queue: it is the rule working. Before the next run: Reset, then a new application |
| Fraud is grey, *Not established* | The application reached the API without a device id or an address, or the hash key or the count store failed; the application went ahead anyway | Nothing on stage. Afterwards, the intake Lambda's log says why (`velocity not captured`) |
| The review screen says *We couldn't confirm a live person* | The face check did not pass (light, angle, distance) | *Redo the selfie video*. Submitting anyway is allowed: the bank asks for it again, and a third failure goes to a person |
| Decided in more than 90 s | Cold start or a slow agent | Say the number. Point to the rehearsal p50/p95 in `docs/MEASUREMENTS.md` |
| *Handing off to review* on a queue row | The task token is written a moment after queueing | Wait a few seconds; the console refreshes |
| *Review window expired* on a queue row | The review task timed out (7 days) | Not a stage issue: re-drive the execution afterwards |
| Publish fails with *the published list could not be read* | Parameter Store read failed; nothing was changed | Publish again |
| Publish reports failed events | EventBridge refused some entries | Avoid re-publishing on stage: it re-screens every open account again. Narrate from the accounts that did change |
| Nadia's Phase 1 declined her | A list update was still published from the last run | Reset, start a new application (section 2 step 1) |
| Every case degrades after a deploy | Cedar ENFORCE denying tool calls | Not fixable on stage; continue on rehearsal cases. After: `make deploy ENFORCEMENT=LOG_ONLY` |
| The API or the network is down | — | Play the rehearsal recording |

---

## 5. Switching Cedar from LOG_ONLY to ENFORCE

The policy engines start attached in LOG_ONLY: every tool call is evaluated and logged, none is
blocked. Switching blind is the one way this control fails open in practice — a misspelt principal
under ENFORCE denies every call, and the clean case never opens an account.

1. Run the clean case (any US persona) end to end, and Act 2's review path, in LOG_ONLY.
2. `make check-policies` (reads the policy engines' CloudWatch decision metrics and denied spans):
   - exit 0 — every evaluated call was allowed; engines with no traffic are listed as unverified
   - exit 1 — something would have been denied; fix the policy or the principal, then repeat
   - exit 2 — nothing was evaluated; run the clean case first
3. `make deploy ENFORCEMENT=ENFORCE`. This moves the Gateway attachment and every policy together,
   and the audit records pin the mode they were decided under.
4. Run the clean case again and confirm it opens an account. `make check-policies` again.

Back out with `make deploy ENFORCEMENT=LOG_ONLY`. Decide before the day, not on it: a deploy takes
minutes.

---

## 6. Re-driving a failed Phase 2

Phase 2 is started with the provisional decision id as its execution name, so a duplicate start
is refused — and so is a second start after a failure, for 90 days. The account stays open with
its provisional limits throughout, and the case stays on the `phase2Pending-index` GSI.

- **Preferred — redrive the failed execution** (same execution, resumes from the failed state;
  Standard workflows, within 14 days):

  ```bash
  aws stepfunctions redrive-execution --execution-arn <the failed Phase 2 execution ARN>
  ```

- **Otherwise — a new execution with a new name and the same input:**

  ```bash
  aws stepfunctions start-execution \
    --state-machine-arn arn:aws:states:us-east-1:<ACCOUNT>:stateMachine:kyc90-phase2 \
    --name <provisionalDecisionId>-r1 \
    --input '{"applicantId": "APP-...", "provisionalDecisionId": "<provisionalDecisionId>"}'
  ```

The lift is conditional on the case still being PROVISIONAL, so a re-drive cannot lift an account
Phase 3 restricted in the meantime, and the account platform refuses any lift below a hold it
does not release (D-21).

Find stuck cases: query `phase2Pending-index` for `phase2Pending = PENDING`, oldest
`provisionalDecidedAt` first.

---

## 7. Removing the demo controls

The `/demo/*` routes let a presenter freeze any open account. Anything that is not a demo account
deploys without them:

```bash
make deploy DEMO_CONTROLS=false
# or only the API stack:
cd infra && npx cdk deploy KycApi -c demoControls=false -c policyEnforcement=LOG_ONLY
```

This removes the demo Lambda, its routes, its permissions and the API's PUT method. The console's
Demo controls then report that the demo routes are not deployed. Remove the `presenters` group
members too.

---

## 8. After the show

1. Reset the demo controls. Decide any open reviews.
2. If the load test ran: `scripts/loadtest.py cleanup --run <run> --apply`.
3. Export the executions while they exist (`make export`; Step Functions keeps 90 days).
4. Evidence and decision records from a demo deployment are locked in GOVERNANCE mode for one
   day (D-35): after that they can be deleted, before it an administrator can remove them with
   the governance bypass. A deployment with `EVIDENCE_LOCK=COMPLIANCE` seals every record,
   rehearsal and load test included, for seven years, and nothing can undo that.

---

## 9. What only a live rehearsal can confirm

The offline evals run the real tools and the real rules; they cannot run the parts that need AWS:

- the agents calling their tools through the Gateways, under Cedar, within their time limits
- Textract reading the generated JPEG well enough for the MRZ to parse, and reading a webcam
  photo of the printed page as well, under the stage lighting (if it does not, upload the JPEG
  on stage: the checks are the same)
- Rekognition matching each presenter's face to the photo on their document
- the end-to-end time to decision, and its p95 (`make measure`)
- EventBridge fan-out and Phase 3 timing for the list update
- the device and network counts (D-41): `kms:GenerateMac` from the intake Lambda, Fraud's lane
  green with *1 person from this device* after a Reset, and the count behind the venue network,
  where the whole room shares one public address
- the reviewer's rationale in the sealed record (D-42): decide Act 2, then open the case's
  *Decisions* tab and its *Latest sealed audit record*
- the Backstage timeline during a live run end to end; it has been checked against a real
  finished case and with recorded histories, not yet while a run is in flight
- the agent lanes with real replies (D-40): each *Said* within its four lines, Act 4's sentence
  whole, and the Cedar line showing the mode from the start of a run (needs KycApi deployed with
  the Gateways' mode: `make deploy`)
- whether AgentCore's own trace view in CloudWatch now shows each harness's model turns and tool
  calls. In September the build found no agent spans (D-38). Ten minutes after a rehearsal run;
  if they are there, that is the honest way to show an agent's internal steps
- the screens in front of a few real people: the cold read behind D-40 was an AI viewer, on
  made-up cases and a mock
- the Cedar decisions (`make check-policies`)
