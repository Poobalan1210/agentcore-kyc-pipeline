# Decisions taken while building

Decisions that changed the build, with the reasoning that produced them. Each entry records
what was rejected as well as what was chosen, because the rejected option is usually the more
useful half a year later.

Spec-level decisions live in the D-table in `requirements.md`. This file carries the ones that
need more room than a table cell, plus the production guidance that differs from what the proof
of concept actually does.

---

## D-14 — Biometric data: this POC runs opted in

**Decided:** proceed opted in, disclose on a slide.

### What the exposure actually is

AWS Rekognition may use images and video submitted to the Face Liveness API to improve its ML
services (F-45). The only mechanism AWS offers to stop this is an **AWS Organizations AI
services opt-out policy** — an Organization-level policy type. Account `633898796092` is
standalone: `DescribeOrganization` returns `AWSOrganizationsNotInUseException`, so the policy
type is not available to it at all (F-73).

So the choice was not *how* to opt out. It was whether to restructure the account to gain the
ability to.

### Why we proceeded anyway

Three things bound the exposure:

1. **Only the presenters' own faces are ever captured** (R36.1, R36.6). No attendee's face, at
   any point. The people bearing the risk are the people who chose it.
2. **It is confined to the Rekognition path.** Bedrock does not use customer inputs to train
   models, so no document text, no applicant attribute, and no screening result is affected.
   This matters because it is the first question anyone asks after hearing the disclosure.
3. **Every other applicant attribute is synthetic** (R17.7). The face is real; the identity
   attached to it is not.

Against that, creating an Organization to gain one policy type is disproportionate for a
demo — it changes billing structure and governance for the account.

### A correction to my own earlier reasoning

I initially recorded that creating an Organization "is not cleanly reversible." **That was
wrong**, and the record should say so rather than quietly drop it. Deleting an organization is
a supported operation: it removes the management account from the org and returns it to
standalone. The genuine reason for choosing to proceed is **proportionality**, not
irreversibility. Overstating the cost of the rejected option is a bad way to arrive at a right
answer.

### Three obligations that are not optional

Choosing to proceed converts the exposure from a gap into an owned risk **only if** all three
hold. Each exists because a specific, identifiable failure mode is otherwise live.

**1. The disclosure goes on a slide, not into the spoken narration.**

Required wording, four seconds to read:

> Real face, synthetic identity. Our copies deleted in 24h.
> AWS may retain theirs — this POC did not opt out. Production would.

re:Invent sessions are recorded and published, and clips circulate without their surrounding
audio. A spoken caveat does not survive that; an architecture slide does. If the disclosure is
verbal only, **AC-45 fails** — this is a criterion, not a preference.

**2. Every deletion claim is scoped to our bucket.**

Our lifecycle rule empties `kyc90-demo-biometric-633898796092` within 24 hours, and that is a
real control. But Rekognition keeps reference and audit images in its own service account, and
those copies are outside our reach. An audience hearing "the images are deleted" understands
"the face is gone," and that is false.

Say **"our copies."** Two words. This is the failure that a compliance reviewer catches,
because "deleted" is a word they hold people to — and this talk's entire thesis is that the
reference architecture under-specifies exactly this kind of claim. Getting it wrong here would
be self-inflicted.

**3. Both presenters consent in writing, to this specifically.**

Consent to appear in a demo is **not** consent for AWS to retain your biometrics and use them
for service improvement. Two presenters means one of them would otherwise be having this
decided for them. The written consent must name the retention and service-improvement use, and
is retained alongside the biometric consent records (AC-45).

### Production guidance — the opposite of what we did

**Create the Organization and attach an `AISERVICES_OPT_OUT_POLICY` covering Rekognition
before any liveness check runs.** R17.9 holds for production without modification.

The POC's choice reflects proportionality for a demo with two consenting subjects. It is not a
recommendation, and it should not be read as one from the stage. Saying "production would" out
loud is what keeps the disclosure honest rather than merely complete.

### Status of each obligation

| # | Obligation | Type | Status |
|---|---|---|---|
| 1 | Disclosure slide, wording above | Deck | **Wording fixed here; slide to be built in the deck (task 13.6)** |
| 2 | Deletion claims scoped to "our copies" | Spec + narration | **Done in spec** (R36.2, R36.5, AC-36) |
| 3 | Written consent from both presenters, naming AWS retention | Human action | **OPEN — cannot be discharged by the build. Blocks G21, not the code** |

Obligation 3 is a human action and is deliberately left open. It is recorded here rather than
marked done, because a build cannot sign a consent form on someone's behalf.

**Traces to:** R17.9, R36.1, R36.2, R36.5, R36.6, F-45, F-73, AC-36, AC-45, task 1.7, G21


---

# Branch `reinvent-ready` — plan v2

The entries below were taken together, to make one claim true end to end: **the tools record
facts, the rules decide on facts, and the agents explain.** Each names what was rejected.

---

## D-15 — Tools record facts; the rules read nothing an agent says

**Decided:** every evidence tool reads its inputs from the case record and records its result
there (`tools/kyctools/case_record.py`). An agent passes only the `applicantId`. The decision
engine reads the recorded facts (`decision-engine/facts.py`); agent replies reach it only as an
availability signal — did the agent run and return something. The prose parser (`signals.py`)
and the output contracts are gone from the decision path.

**Why:** measured in this build, an agent told a tool was UNAVAILABLE replied "which means no
match was found". When the rules read prose, that sentence could approve someone. Reading facts,
the rules see `performed: false` and route to a person; the sentence becomes harmless narrative.
A prompt-injected agent can still ask to screen "John Doe"; the tool screens the applicant on the
record, and records the name it was asked for beside the result (`agentNameMatchesRecord`).

**Rejected:** structured output contracts as the data interface (across two live runs with an
identical prompt, fixture and model configuration, the Documents agent reported the same finding
once as `{"confidence": 0.96}` and once as a sentence); stronger prompts (no prompt makes a
model's report of a tool result the same thing as the tool result).

**Evidence:** `make evals` — the `narrative` suite decides every persona again with the agents'
replies replaced by text written to mislead; the `unavailable` suite forces each tool down while
the agent claims all is well.

## D-16 — One deterministic sanctions screen, in Phase 1

**Decided:** `sanctions_screen` runs exact and variant matching in Phase 1, before the account
opens, and the same tool re-runs in Phase 2 when the list changed and in Phase 3 on a list update.

**Why:** sanctions breaches are strict liability, and variant matching is plain code that takes
milliseconds. Leaving transliteration and name order to Phase 2 meant opening the account first.

**Rejected:** exact-only in Phase 1 and fuzzy in Phase 2 (the account is already open by then).

## D-17 — The Router becomes the Briefing agent

**Decided:** the seventh agent writes the reviewer's briefing from the recorded facts. It runs only
on the review path and never decides. Jurisdiction comes from intake, not from a model.

**Why:** the Router sat first on the 90-second path, failed tool calls on Nova Micro, and decided
something code already knew. A reviewer reading twenty raw facts benefits from a paragraph.

## D-18 — Only a CONFIRMED match is prohibited; reviewers clear the rest for good

**Decided:** CONFIRMED (name and date of birth agree) makes the tier PROHIBITED and cannot be
approved — not by the rules, not by a reviewer (`review/decisions.py` refuses it). PROBABLE and
POSSIBLE go to a person; an approval records a disposition per listing, written in the same
conditional write as the decision, and the rules drop a dispositioned alert in every later phase.
Two known dates of birth that differ are an exclusion, reported with its reason.

**Why:** 90–95% of real alerts are false positives. Without dispositions, Phase 2 and every list
update would send the same cleared customer back to the queue; without the CONFIRMED guard, one
click could approve a listed person.

**Known limit:** a disposition clears a listing by id, across list versions. Here an update can
never change an existing listing (update ids are reserved, D-19); with a real list, a changed
listing should re-raise its alert.

## D-19 — Runtime controls in Parameter Store, failing in opposite directions

**Decided:** `/kyc90/demo/faults` (tools reported unavailable, agents given less time) fails
**open**: unreadable means no faults. `/kyc90/screening/list-overlay` (a published list update)
fails **closed**: unreadable means screening is unavailable. Both are re-read every few seconds,
because the Gateway invokes a published alias and an environment variable edit never reaches it.
Update ids are reserved to SYN-0900–SYN-0999; a clash with a base listing makes screening
unavailable rather than shadowing a designation. The publish route reads the current overlay
strictly — a blip must not publish the new listing alone and delist the others — and waits out
the tools' cache before fanning out.

**Rejected:** environment variables (need a redeploy to flip on stage); a fail-open overlay (an
unreadable list update would screen against a stale list).

## D-20 — Cedar stays LOG_ONLY until its decisions are checked

**Decided:** one flag, `-c policyEnforcement=LOG_ONLY|ENFORCE`, moves both the Gateway attachment
mode and every policy's enforcement mode. `scripts/check_policy_decisions.py` reads the policy
engines' CloudWatch decision metrics (and, optionally, denied spans) and exits 0 only when traffic
was evaluated and nothing was denied, failed a policy, or fell to default deny.

**Why:** the engine denies by default. A misspelt principal under ENFORCE denies every tool call,
every case degrades to review, and the clean case never opens an account on stage. Engines with
no traffic are reported as unverified rather than passed.

## D-21 — Holds: a later phase cannot lift an earlier restriction it did not place

**Decided:** a restriction can carry a hold, named by the event that placed it, with a floor
posture. A lift that would go below an unreleased hold is refused (`AccountOnHold`). Phase 3
places holds; a Phase 3 clearance releases its own.

**Why:** the race: a list update freezes or restricts an account while Phase 2 for the same
customer is still running; Phase 2 then decides LOW on older evidence and lifts to standard.

**Rejected:** an account version check on lifts (deposits bump the version, and the window
remains); conditions on the case record alone (the platform lift happens before the case write).

## D-22 — A second review waits for the first

**Decided:** every entry into review goes through `ReviewGate`: if the case already has an open
review, the new one waits (ten minutes, up to a week) rather than replacing its task token. A case
frozen or decided meanwhile skips the review it no longer needs.

**Rejected:** overwriting the open review's token (orphans a live execution); dropping the event
(loses a risk signal).

## D-23 — A screening of an outdated list is not a screening

**Decided:** the state machines read the current list version (base plus any published update)
immediately before deciding and pass it to the rules. A screening fact recorded against another
version is `LIST_OUTDATED`, which no phase can decide on. Phase 2 re-screens when the list moved
since Phase 1. Phase 3 for a case with no account yet does nothing (`NoOpenAccount`): Phase 2's
check covers it.

**Rejected:** Phase 3 waiting for the account to open (a lift could land before the re-screen);
re-reading the list just before Phase 2's tier (more reviews, and Phase 3 already covers updates
published during Phase 2).

## D-24 — Review targets per phase

**Decided:** what a review decision does depends on its phase: Phase 1 APPROVE opens with limits;
Phase 2 APPROVE lifts, STEP_UP keeps the account restricted, REJECT freezes; Phase 3 APPROVE
restores the customer's day-zero posture and REJECT exits (freezes). Phase 3 has no step-up.

**Why:** every console review used to be filed as Phase 1, so a Phase 2 or Phase 3 decision
checked and wrote the wrong slot.

## D-25 — The tipping-off guard: fixed wording, chosen by account state

**Decided:** before any message reaches an applicant it is checked for language that would say why
an account is limited (sanctions, screening, risk, compliance, watch lists, investigation…). A
message that trips it is replaced whole by fixed wording chosen by account state
(`api/notifications.py`). The applicant sees a status, never the rule or the reason; a reason code
only for a step-up, only from an allowlist about what they sent. Their account view leaves out
holds and platform events (both name the event that caused them). Once an account exists, the
applicant is told about the account, not the case behind it. The applicant app's own wording lives
in one file that a test holds to the same guard.

**Rejected:** redacting the offending phrase (a message with a hole in it still says something was
removed); trusting the Notify agent's prompt alone (it is never given the reason, but the guard is
the deterministic second line).

## D-26 — Who may read and write what, at the API

**Decided:** group membership is parsed into whole names (the HTTP API renders an array claim as
`"[reviewers applicants]"`, and a substring check let `notreviewers` in). An applicant reads only
ids built from their own `sub`; anyone else gets 404, not 403. A case may list only documents the
applicant uploaded, to their jurisdiction's bucket, under their own prefix. A resubmission after a
step-up keeps the previous attempt in `phase1History`. A liveness session is bound to the case at
creation and its result accepted once; failures are counted; no liveness result is accepted once
the application is submitted. Intake no longer writes audit version pins it could only invent.

## D-27 — Presenter controls are a demo capability, deployed on purpose

**Decided:** `/demo/*` routes for the `presenters` group, deployed only with
`-c demoControls=true`, granted by hand. The list update fans out one HIGH event per open
account — never CRITICAL, which freezes regardless of screening — so the screening decides. The
board's time to decision is computed from the case record's own timestamps.

**Why stated here:** a presenter can freeze any open account. That is acceptable for a demo
account and nothing else; `-c demoControls=false` removes the routes and their permissions.

## D-28 — The audit record says what a person approved past

**Decided:** decision records carry `executionName` (so the reviewer console's timeline resolves —
it never could, because no execution name was recorded), and for reviewed decisions
`engineOutcome`, `reviewReason` and `clearedAlerts`; a Phase 2 record names the provisional
decision it supersedes.

**Why:** "a reviewer approved" without what the rules had concluded and which alerts the approval
cleared is not an explanation an examiner can use.

## D-29 — Velocity is "not established", not zero

**Decided:** this build captures no device or IP identifier at intake, so the velocity tool records
`performed: false, status: NOT_ESTABLISHED`. The rules accept it as not established; they never
read it as "no reuse observed".

**Rejected:** recording zero counts (true only if someone looked).

**Since D-41:** intake captures both identifiers for applications made in the console. When it
cannot, NOT_ESTABLISHED is still the result, for the reason above.

## D-30 — Offline evals run the real tools and the real rules

**Decided:** `evals/` drives the six tools through the Gateway's Lambda entry point and the rules
through their own handler, over fake AWS services, with the exact event the state machines build
(a test compares the keys with the deployed definitions). What is assumed is stated: the face
on the document matches the liveness frame, which the demo makes true by printing the presenter's
photo on their persona's document.

**Why:** the claims the talk makes — the rules decide on facts, the narrative cannot move them, a
check that did not run never approves, the same facts give the same answer a thousand times — are
properties, so they are tested as properties, before every rehearsal.

## D-31 — No script fabricates a decided case

**Decided:** demo cases are seeded by running the personas through the real flow at rehearsal;
`scripts/prepare_demo.py check` confirms where each stands. The load test replays one fresh
rehearsal's real evidence through the real pipeline, labelled, and cleans up after itself except
for the sealed audit records, which Object Lock keeps.

**Rejected:** writing decided case records directly (an open account with no live face behind it
is exactly what Phase 1 exists to prevent, and a demo built on one would demonstrate nothing).

## D-32 — Demo personas meet the list on purpose, and only in the US

**Decided:** three US personas collide with the synthetic list by design — a name overlap with a
listing that has no date of birth (review, then cleared), an exact namesake born 26 years later
(excluded, approved), and a customer a later list update names exactly (frozen in Act 3). Their
screening outcomes are pinned by test against the real matcher, and the applicant app's persona
picker is held equal to the documents by test.

**Why US only:** outside `DECIDED_JURISDICTIONS` a case goes to a reviewer for the jurisdiction
before screening can decide anything, so a screening scenario there would never play out on stage.

## D-33 — Stage 2 creates the Cedar policies with boto3, not CloudFormation
**Decided:** `make stage2` runs `policies/deploy_stage2.py`, which creates each statement with
`CreatePolicy` in the requested enforcement mode and moves existing ones with `UpdatePolicy`.
`KycPolicies` stays in the CDK app so the tests check the same shape, and is never deployed.
`make destroy` deletes the policies on the `kyc90_policy_*` engines first, because an engine that
still has policies cannot be deleted.
**Why:** `cdk deploy KycPolicies` failed again on the first deploy into a fresh account. Policy
names are unique per account, not per engine, and all four engines asked for the same bare name
at once: one call succeeded, three got `ConflictException`. `deploy_stage2.py` names each policy
with a hash of its statement, and every statement names its own jurisdiction's Gateway, so no two
names collide. The Makefile had never called it, so `make deploy` could not finish.

## D-34 — Guardrails are scoped to the tools that carry the field they scan
**Decided:** the prompt-attack guardrail and both content filters are scoped to
`action in [...]` the three screening tools, the only Gateway actions that declare `input.name`.
PII suppression is not deployed: it reads `context.output.result`, no Gateway tool declares an
output schema, and `pii_suppression_statements` returns nothing until one does.
**Why:** `CreatePolicy` now checks a guardrail's field against every action in scope, and the
bare `action` includes the `CallTool` parent, which declares no fields, so all 20 guardrail
statements failed with "context field-path argument must be declared on every action the rule is
scoped to". The other three tools take only the system-issued applicantId, so scoping loses no
coverage. The PII statements used to be accepted on an unscoped `action` but scanned a field no
tool returned, so leaving them out removes nothing that worked.
**Open:** PII suppression needs output schemas on the tools. That changes what the Gateway
returns to the agents, so it needs its own live run before the talk relies on it.

## D-35 — A demo deployment locks evidence for one day, not seven years
**Decided:** the evidence buckets' default retention and the audit writer's lock follow one
switch, `evidenceLock`. `DEMO` (the default whenever demo controls are on) is GOVERNANCE for one
day; `COMPLIANCE` (the default with `DEMO_CONTROLS=false`) is COMPLIANCE for seven years. A
stack built without a choice gets COMPLIANCE. Object Lock stays enabled on the buckets either
way, because it cannot be added later, and `make verify` checks whichever lock was deployed.
**Why:** a demo uploads documents printed with the presenter's real face, and it decides no real
person's application. Locking those for seven years, beyond anyone's reach, serves no regulator
and contradicts R36's rule that a real face stays deletable. GOVERNANCE still refuses an
ordinary delete, so the lock can be shown, while an administrator can remove an object at once
with the bypass and anyone can after a day.
**Not undone:** a retention change applies only to objects written after it. In account
333105300941, the four `_verify/` probes written under COMPLIANCE before this change stay until
2033-09-30, so those four evidence buckets cannot be deleted before then. They are 46 bytes each.

## D-36 — The demo's face check moves, it does not flash
**Decided:** every Face Liveness session names its challenge (`Settings.ChallengePreferences`),
from one switch, `livenessChallenge`. A demo deployment (demo controls on) asks for
`FaceMovementChallenge`: the applicant moves toward the camera until the face fills the oval, and
that is all. Anything else asks for `FaceMovementAndLightChallenge`, the service default, which
adds a sequence of full-screen flashing colours. The API falls back to the light challenge for an
unset or unknown value, and the stack refuses an unknown one at synth.
**Why:** on stage the flashes wash a projector in colour, warn the room about photosensitivity
before the check can start, and add several seconds to the step the audience watches. The
console's face check also starts as soon as the camera is ready (no start screen), centred under
the webcam so the presenter faces the camera.
**The cost, stated:** AWS recommends the light challenge for the highest accuracy, and says the
choice should weigh expected attacks and the false accept and reject rates a use case can bear.
A production onboarding flow should keep it (`LIVENESS_CHALLENGE=FaceMovementAndLightChallenge`,
the default with `DEMO_CONTROLS=false`). The confidence threshold (80) is unchanged either way.

## D-37 — The customer's screen is a bank's app, in a bank's order
**Decided:** the phone shows one thing, a bank's app ("AnyCompany Bank", the fictitious name AWS
uses in its own material), in the order digital onboarding uses: a welcome with explicit consent
to the face check, the declared details, the ID document, the selfie video, one review of all of
it, then Submit. Nothing is sent to the bank before Submit, which is when the four agents start;
a step-up goes back through the review. The ID is photographed with the laptop's webcam on the
same full-window stage as the selfie, with an upload kept as the fallback. The presenter picks
the persona from the console's New application menu, beside the phone rather than on it. The
account is a home screen with four one-tap actions the account platform allows or refuses, and
Act 3 publishes a batch of four synthetic listings, of which only SYN-0901 names a customer.
**Why:** the demo read as staged, and the reasons were on the customer's screen: a persona
dropdown, a hint naming the file to upload, the face check before any details, a timer counting
on the phone, a "try it" form, and a list update naming exactly one person. No bank app has
any of those. Every check behind the screens is the same real one as before; the timer is the
bank's figure and stays on the console, computed from the case record.
**The crop:** `rekognition_compare_faces` compares the largest face in the document image with
the liveness frame. A webcam photo of a passport held up also catches the presenter, so only the
inside of the guide is kept, and the guide has the generated card's proportions (520 × 430).
The crop leaves out the page's specimen banner, which sits above the card; the card's own
authority field still reads "(SPECIMEN)", and the upload is still marked synthetic in its S3
metadata, so the evidence never presents as a real document.
**Not settled by this:** the welcome screen's consent is the app's own record, shown to the
audience. It does not replace the presenters' signed consent (D-14). Whether Textract reads a
webcam photo of the printed page as well as it reads the JPEG is a rehearsal question
(runbook section 9).

## D-38 — The live view draws the system's own records, lane by lane
**Decided:** the console's process side has a second view, Backstage, beside the checklist
(Story). It draws each phase as a timeline on its own clock: a bar for every state Step Functions
entered, the four evidence agents side by side in their Parallel state, a dot where each tool's
fact landed on the case, the rules, the sealed record and the account, and a feed of every write
underneath. It is the default for reviewers. To follow a run while it runs, each phase's execution
is findable from the case: Phase 1's claim records `phase1Execution`, Phase 3's first state
records `phase3Execution` (conditional, and any failure carries on), and Phase 2 already runs under
the provisional decision id. The trace route returns all three, state by state, with what each
task called (`lambda:invoke` and the function, or the harness), and each agent run records the
model its task asked for.
**Why:** the checklist says what happened; it cannot show that four agents ran at once, what ran
in the background after the account opened, or which service did each step. Every mark on the
timeline comes from the execution history or the case record, so nothing is simulated, and the
link to the Step Functions console lets anyone check the same run there.
**The cost, stated:** while a phase runs, each open console reads its execution history every two
seconds (a finished history is kept per warm Lambda container). A task's parameters carry case
data, so only the integration and the function or harness name are read out of them. What the
system does not record is not drawn: the harness emits no agent or tool spans (`audit/spans.py`),
so an agent's lane is its task from invoke to answer, never its model turns, and the Cedar decision
on each tool call is in CloudWatch, not here. A case that ran before this change has no Phase 1
run name, so its Phase 1 timeline is unavailable; its facts are all on Story.

## D-39 — A customer message says the account's state, not its figures
**Decided:** the guard in `api/notifications.py` reads the Notify agent's reply for its message
(the `body` field when it answers in the fields its prompt asks for) and, after the tipping-off
check, holds it to what the bank can vouch for from the account's state alone. A message that
says something the state rules out (a restricted account called fully open, an open one told a
limit still applies, a frozen one told what it can do) is replaced with the state's fixed wording
(`CONTRADICTS_ACCOUNT`), and so is one that quotes a figure or lists limits
(`UNVERIFIABLE_DETAIL`). The account page shows the platform's own list. The reviewer sees why
each message was replaced. The guard runs on every read, so messages already stored are fixed too.
**Why:** after the Notify fix the agent's replies reached the inbox verbatim: a JSON object, with
`\n` escapes. Read for their message, four of the first six were still wrong. Given six
restrictions, two first messages listed two and three of them as "the current limits", and two
messages to accounts that were fully open said a $2,000 deposit ceiling still applied (the
account view always carries the configured ceiling) or that international transfers were
blocked. A wrong limit told to a customer by their bank is a false statement.
**Rejected:** only reading the `body` (the wrong claims stay); always sending fixed wording (the
agent would do nothing); checking the agent's list against the account's restrictions (a
paraphrase does not match deterministically). The price: the one first message that listed all
six correctly is replaced too.
**The cost, stated:** the Notify prompt still asks for "exactly which limits apply ... using the
amounts", so most first messages are replaced until the prompt is changed. That change is a new
harness version (`KycHarnesses`).

## D-40 — An agent's lane shows what its tool recorded beside what the agent said
**Decided:** on Backstage each agent lane is labelled as an AgentCore harness. The dot where its
tool's fact landed is coloured by what the fact records, so a check that did not run is never
green. The bar is split at that dot: before it the harness is calling its tool through the
Gateway; after it the model is writing its reply. Beside the bar, *Recorded* is the fact and
*Said* is the agent's own words, quoted from its first word. One line above Phase 1's lanes says
what the marks mean and which mode the Gateway's Cedar policy engine is in. LOG_ONLY reads
"logging, not blocking". The mode comes from the case's decision record (`policyEngineVersion`),
or, while a run has no record yet, from the deployment: the review Lambda gets
`POLICY_ENFORCEMENT`, the value the Gateways are attached in, and the trace returns it as
`policyEnforcement`. The ids, model, tokens, harness version, Gateway and policy engine are in the
lane's drawer. A fact is drawn on a lane only if it landed while that agent ran. Every re-screen
rewrites the screening fact, so Phase 1's lane says its fact was since replaced rather than set a
later result beside Phase 1's words. The review console shows the briefing and each reply as
fields, not raw JSON. *Large text* in the bar zooms the process side, the review console and a
case's page by 1.35; the phone and the bar keep their size.
**Why:** a cold read of the old screens as three people (a cloud architect new to AgentCore, an
AgentCore builder, a bank compliance lead) found AgentCore doing nothing visible and each agent
reading as a tool wrapper. Act 4 was ambiguous: a green dot beside a red *Not performed*, and the
agent's words nowhere. With a mock of these lanes all three could describe the steps, and Act 4's
point landed in about fifteen seconds: screening did not run, the agent still said no match was
found, and the rules sent the case to a person. In fifteen seconds viewers took in only the dot
colours, *Not performed* against "no match", and *Refer to a person*, hence one line of legend and
everything else in the drawer. Browser zoom is no answer for the room: it narrows the window,
hides the AgentCore line in the bar and squeezes the timeline.
**Rejected:** drawing model turns or tool arguments (the harness emits no agent or tool spans,
D-38, so they would be invented); an excerpt of the reply (it read as cherry-picked); "one
allowed tool" (Fraud also has Code Interpreter, which is outside the Gateway: the drawer lists
the Gateway tools and the built-in apart); a bare `LOG_ONLY` badge (read as "the control is
switched off").
**The cost, stated:** the split is inferred from two timestamps, the fact's `recordedAt` and the
end of the harness task. Which model turns fell on either side is not known. The rules start only
when all four agents have answered, so reply time after a fact lands delays the decision even
though the rules never read the reply; the reviewer does. The Cedar decision on each call is in
CloudWatch, not here. The console's list of each agent's tools mirrors `infra/kyc/config.py` and
`harnesses_stack.py` by hand. Before a record exists, the mode shown is the API's, so it is right
only when KycApi was deployed with the Gateways' `policyEnforcement` (`make deploy` does both). An
API deployed before this change reports no mode, and the line names Cedar without one. With
Large text, Phase 1 is taller than a 1080p screen. The cold read was an AI viewer, on made-up
cases and a cleaner mock than the full screen; a dry run with a few people is the check that
counts.

## D-41 — Velocity counts people per device and network, from intake, and Reset clears it
**Decided:** the console sends a random device id with every application (`crypto.randomUUID()`,
kept in the browser's local storage). Intake (`api/velocity.py`) hashes it, and the caller's
network as API Gateway saw it (an IPv4 address, or an IPv6 /64), with a KMS HMAC key. It stores
both hashes on the case as `intake` and writes one row per person under each into the velocity
store, keyed by a hash of the declared name and date of birth and counted for 24 hours. The Fraud
agent's tool counts those rows, so its fact says how many different people applied from this
device and this network in a day, this application included: five from one device, or ten from
one network, is critical and sends the case to a person. Reset (Demo controls, and
`scripts/prepare_demo.py reset`) deletes every row and touches no case. Capture never stops an
application: without the device id, the address, KMS or the store, the case carries neither hash
and velocity stays NOT_ESTABLISHED (D-29).
**Why:** the Fraud lane was grey on every case because nothing captured a device or IP, so Phase
1's one fraud check never ran. Counting people rather than submissions is what velocity is for:
many identities from one device is the synthetic-identity pattern, one person retrying is not.
It also keeps a presenter's restarts from tripping the rule: the run of show's four personas stay
at four however often they are run. Keyed hashes keep IP addresses out of the case record and the
store; an unkeyed hash of an IPv4 address is reversed by trying all of them.
**Rejected:** counting each submission (one restart on stage would be the fifth); a fingerprinting
library (more invasive than the check needs, and still supplied by the client); raising the
thresholds for the demo (the rule on stage would not be the rule); deleting cases to reset the
counts (the counts live in their own table).
**The cost, stated:** the device id is whatever the browser sends, so cleared storage or another
browser is a new device, and someone who rotates it is caught only by the network count. Behind a
venue's Wi-Fi the whole room shares one public address; only applications count, and only the
presenter applies. The fact keeps its field names, `deviceApplications` and `ipApplications`, and
each counts people. Not yet seen live: `kms:GenerateMac` from the intake Lambda, and the counts
behind the venue network (runbook section 9).

## D-42 — What the screens and records say happened is what happened
**Decided:** three claims made true. The reviewer's rationale is sealed in the decision record
(`decision.rationale`): the audit writer reads it from the review the handler stored
(`<phase>Review`), and only when that review is the one being recorded (same reviewer, same
instant). It never travels with the task token, so it stays out of execution history (R18.8). A
confirmed match's explanation says the decision is flagged for compliance (`notifyCompliance`),
not that anyone was notified, and the account platform's freeze event no longer claims
`complianceNotified`. After three failed liveness checks the record says the case went to a
reviewer, with `alternative_path_offered: false`, and the applicant's review screen shows a
failed face check as failed, with the redo first and Submit still there.
**Why:** each was a claim the system did not keep. The decision form promised the rationale was
"recorded in the audit trail" while it lived only on the mutable case, and a Phase 3 rationale was
deleted when the next lifecycle review was queued. Nothing reads `notifyCompliance`. No other way
to verify is offered to the applicant. On stage, a failed face check showed "Live check complete"
with a tick.
**Rejected:** carrying the rationale in the resume payload (it would sit in execution history for
90 days); sending a notification (a compliance referral with OFAC and SAR deadlines is a feature,
not a wording fix); disabling Submit after a failed check (R30.2: no lockout).

## D-43 — A U.S. applicant gives an SSN or ITIN, and is told why first
**Decided:** two CIP items (31 CFR 1020.220) the build lacked. The Welcome screen shows, for the
U.S. entity, notice that federal law requires the bank to get, verify and record identifying
information, before anything is collected (§ (a)(5)). The details form asks for an SSN or ITIN,
required of a U.S. national applying to the U.S. entity (§ (a)(2)(i)(A)(4)); a foreign national
may rely on the passport already on the case. Intake checks the format (`applicants._tax_id`,
mirrored in the console's `taxId.ts`) and stores the number on its own item (`sk = "TIN"`), never
on the case. Screens show its last four digits. The browser never saves it, so a step-up
resubmitted from a reloaded tab goes without it and the number on file stands. The demo personas
use 987-65-4320 to 987-65-4329, which SSA reserves for advertisements and never issues.
**Why:** a banker would ask where the SSN is, and the rule requires both. The case item is read
whole by the state machines, so anything on it sits in execution history for 90 days and on
Backstage; nothing in the pipeline needs the number.
**Rejected:** the regulation's sample notice word for word (it names what the law is for, in
words the tipping-off guard refuses in anything a customer reads, and the notice is adequate
without them); a `ProjectionExpression` on every case read instead of a separate item (four
state machines and every future read would have to remember it); verifying the number with SSA's
eCBSV (a real service needing enrolment and the applicant's consent; the build checks format
only, and says so).
