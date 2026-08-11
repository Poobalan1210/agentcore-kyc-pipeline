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
