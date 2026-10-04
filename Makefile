# KYC in 90 Seconds
#
# One `cdk deploy` is impossible and this Makefile does not pretend otherwise. Cedar policies
# must name concrete Gateway ARNs and Cedar forbids wildcard resources (F-17), so the Gateways
# must exist before their policies can be written. `make deploy` runs both stages and prints
# why there are two.

SHELL      := /bin/bash
VENV       := .venv
PY         := $(VENV)/bin/python
PIP        := $(VENV)/bin/pip
REGION     ?= us-east-1
ACCOUNT    ?= $(shell aws sts get-caller-identity --query Account --output text)

# Cedar enforcement for both halves at once: the Gateway attachment and every policy. LOG_ONLY
# until scripts/check_policy_decisions.py shows the logged decisions permit the clean case.
ENFORCEMENT ?= LOG_ONLY

# The presenter routes (/demo/*). A presenter can freeze any open account with them, so anything
# that is not a demo deploys with DEMO_CONTROLS=false, which removes the routes and their grants.
DEMO_CONTROLS ?= true

# Evidence retention (D-35): what the buckets default to and what each decision record is
# written under. A demo deployment locks in GOVERNANCE for one day, so its evidence (documents
# carrying the presenter's real face) can be removed. Anything that is not a demo
# (DEMO_CONTROLS=false) gets COMPLIANCE for seven years. EVIDENCE_LOCK=COMPLIANCE|DEMO overrides.
EVIDENCE_LOCK ?= $(if $(filter false,$(DEMO_CONTROLS)),COMPLIANCE,DEMO)

# Face Liveness challenge (D-36). A demo uses the movement challenge (no flashing colours, a few
# seconds quicker); anything else keeps the light challenge AWS recommends for the highest
# accuracy. LIVENESS_CHALLENGE=FaceMovementAndLightChallenge|FaceMovementChallenge overrides.
LIVENESS_CHALLENGE ?= $(if $(filter false,$(DEMO_CONTROLS)),FaceMovementAndLightChallenge,FaceMovementChallenge)

CDK        := PATH="$(PWD)/$(VENV)/bin:$$PATH" npx cdk
CDK_CTX    := -c policyEnforcement=$(ENFORCEMENT) -c demoControls=$(DEMO_CONTROLS) \
              -c evidenceLock=$(EVIDENCE_LOCK) -c livenessChallenge=$(LIVENESS_CHALLENGE)

export CDK_DEFAULT_ACCOUNT = $(ACCOUNT)
export CDK_DEFAULT_REGION  = $(REGION)

# Stage 1 is every stack except the Cedar policies. Naming only KycFoundation here deployed the
# foundation and nothing else, so `make deploy` never produced a working system.
STAGE1 := KycFoundation KycTools KycGateways KycHarnesses KycOrchestration KycIngress \
          KycLifecycle KycAuth KycApi

.PHONY: help venv install test synth deploy stage1 stage2 verify spike clean destroy \
        demo-assets frontend evals measure export check-policies preflight

help:
	@echo "make install      create the venv and install pinned dependencies"
	@echo "make test         the whole test suite, including the synthesised templates"
	@echo "make synth        synthesise CloudFormation for every stack"
	@echo "make deploy       two-stage deploy: stage 1, capture Gateway ARNs, stage 2"
	@echo "                  ENFORCEMENT=ENFORCE switches Cedar from LOG_ONLY (see RUNBOOK)"
	@echo "                  EVIDENCE_LOCK=COMPLIANCE keeps evidence 7 years (default: DEMO, 1 day)"
	@echo "                  LIVENESS_CHALLENGE=FaceMovementAndLightChallenge for the most accurate"
	@echo "                  face check (default for a demo: FaceMovementChallenge, no flashing)"
	@echo "make verify       assert the live account matches the spec (Object Lock, IAM, GSIs)."
	@echo "                  NOT read-only: writes a probe to each evidence bucket. Under DEMO it"
	@echo "                  removes the probes again; under COMPLIANCE they stay for 7 years"
	@echo "make frontend     install and build the console (run it: cd frontend && npm run dev)"
	@echo "make demo-assets  regenerate the synthetic demo identity documents"
	@echo "make evals        offline: every persona's outcome, determinism, narrative-proofing"
	@echo "make preflight    read-only: stacks, demo controls, groups, where each persona stands"
	@echo "make measure      read-only: p50/p95 time to decision and cost from the case table"
	@echo "make export       read-only: export the last day's executions, state by state"
	@echo "make check-policies  read-only: is it safe to switch Cedar to ENFORCE?"
	@echo "make destroy      tear down. Evidence buckets are RETAINED by policy and by Object Lock"

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r infra/requirements.txt
	@$(PY) -c 'import importlib.metadata as m; print("installed: aws-cdk-lib", m.version("aws-cdk-lib"))'

# Tests assert the irreversible properties BEFORE a deploy, because a bucket without Object
# Lock is indistinguishable from one with it until someone tries to delete evidence. The whole
# suite, from the repository root: the graph tests check the tools, the engine and the account
# platform agree with the state machines, which a run of infra/ alone cannot see.
test:
	@AWS_DEFAULT_REGION=$(REGION) $(PY) -m pytest -q -p no:cacheprovider

synth: test
	@cd infra && $(CDK) synth --quiet $(CDK_CTX)

stage1: test
	@echo "=== STAGE 1 — every stack except the Cedar policies" \
		"(Cedar $(ENFORCEMENT), evidence lock $(EVIDENCE_LOCK)) ==="
	@cd infra && $(CDK) deploy $(STAGE1) $(CDK_CTX) --require-approval never \
		--outputs-file cdk-outputs.json

# Stage 2 creates the policies with boto3, not `cdk deploy KycPolicies`: CloudFormation's handler
# for AWS::BedrockAgentCore::Policy failed this stage repeatedly (policies/deploy_stage2.py says
# how). KycPolicies stays in the CDK app so the tests check the same shape; it is never deployed.
stage2:
	@echo "=== STAGE 2 — Cedar policies (Cedar $(ENFORCEMENT)) ==="
	@echo "Two stages because Cedar policies must reference concrete Gateway ARNs and Cedar"
	@echo "forbids wildcard resources (F-17). The Gateway cannot be named before it exists."
	@cd infra && test -f cdk-outputs.json || { echo "run 'make stage1' first"; exit 1; }
	@cd infra && ../$(PY) ../policies/generate.py --outputs cdk-outputs.json \
		--out ../policies/generated
	@AWS_DEFAULT_REGION=$(REGION) $(PY) -u policies/deploy_stage2.py --enforcement $(ENFORCEMENT)

deploy: stage1 stage2
	@echo "both stages complete"

# Writes a small probe object to each of the four evidence buckets and proves the delete is
# refused. Under the DEMO lock it then removes each probe with the governance bypass. Under
# COMPLIANCE nobody can delete them, or the buckets that hold them, for seven years: run it once
# per account, knowingly.
verify:
	@cd infra && EVIDENCE_LOCK=$(EVIDENCE_LOCK) ../$(PY) verify_foundation.py

frontend:
	@cd frontend && npm ci && npm run build

spike:
	@cd spike && ../$(PY) sfn_gate.py

demo-assets:
	@$(PY) demo-assets/gen_documents.py

# Offline: the real tools and rules over fake AWS services. Needs no account.
evals:
	@$(PY) evals/run_evals.py

# The rest read the deployed account and change nothing.
preflight:
	@AWS_DEFAULT_REGION=$(REGION) $(PY) scripts/prepare_demo.py check

measure:
	@AWS_DEFAULT_REGION=$(REGION) $(PY) scripts/measure.py

export:
	@AWS_DEFAULT_REGION=$(REGION) $(PY) scripts/export_executions.py

check-policies:
	@AWS_DEFAULT_REGION=$(REGION) $(PY) scripts/check_policy_decisions.py --spans

# The policies go first: they are not in any stack, and an engine with policies on it cannot be
# deleted.
destroy:
	@echo "Evidence buckets and the case table are RETAINED: destroy does not delete them."
	@echo "Objects written under COMPLIANCE cannot be deleted until their retention ends."
	@AWS_DEFAULT_REGION=$(REGION) $(PY) policies/sweep.py
	@cd infra && $(CDK) destroy $(STAGE1) $(CDK_CTX) --force

clean:
	@rm -rf infra/cdk.out infra/.pytest_cache infra/**/__pycache__ infra/__pycache__
