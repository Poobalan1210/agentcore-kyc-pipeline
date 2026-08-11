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
CDK        := PATH="$(PWD)/$(VENV)/bin:$$PATH" npx cdk
REGION     ?= us-east-1
ACCOUNT    ?= $(shell aws sts get-caller-identity --query Account --output text)

export CDK_DEFAULT_ACCOUNT = $(ACCOUNT)
export CDK_DEFAULT_REGION  = $(REGION)

STAGE1 := KycFoundation
STAGE2 := KycPolicies

.PHONY: help venv install test synth deploy stage1 stage2 verify spike clean destroy demo-assets

help:
	@echo "make install      create the venv and install pinned dependencies"
	@echo "make test         pytest against the synthesised template (run before every deploy)"
	@echo "make synth        synthesise CloudFormation"
	@echo "make deploy       two-stage deploy: stage 1, capture Gateway ARNs, stage 2"
	@echo "make verify       assert the live account matches the spec (Object Lock, IAM, GSIs)"
	@echo "make demo-assets  regenerate the synthetic demo identity documents"
	@echo "make destroy      tear down. Evidence buckets are RETAINED by policy and by Object Lock"

venv:
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv
	@$(PIP) install --quiet --upgrade pip
	@$(PIP) install --quiet -r infra/requirements.txt
	@echo "installed: $$($(PY) -c 'import importlib.metadata as m; print(\"aws-cdk-lib\", m.version(\"aws-cdk-lib\"))')"

# Tests assert the irreversible properties BEFORE a deploy, because a bucket without Object
# Lock is indistinguishable from one with it until someone tries to delete evidence.
test:
	@cd infra && ../$(PY) -m pytest

synth: test
	@cd infra && $(CDK) synth $(STAGE1)

stage1: test
	@echo "=== STAGE 1 — foundation through orchestration ==="
	@cd infra && $(CDK) deploy $(STAGE1) --require-approval never \
		--outputs-file cdk-outputs.json

stage2:
	@echo "=== STAGE 2 — Cedar policies ==="
	@echo "Two stages because Cedar policies must reference concrete Gateway ARNs and Cedar"
	@echo "forbids wildcard resources (F-17). The Gateway cannot be named before it exists."
	@cd infra && test -f cdk-outputs.json || { echo "run 'make stage1' first"; exit 1; }
	@cd infra && $(PY) ../policies/generate.py --outputs cdk-outputs.json \
		--out ../policies/generated
	@cd infra && $(CDK) deploy $(STAGE2) --require-approval never

deploy: stage1 stage2
	@echo "both stages complete"

verify:
	@cd infra && ../$(PY) verify_foundation.py

spike:
	@cd spike && ../$(PY) sfn_gate.py

demo-assets:
	@$(PY) demo-assets/gen_documents.py

destroy:
	@echo "Evidence buckets have RemovalPolicy.RETAIN and Object Lock in COMPLIANCE mode."
	@echo "They will NOT be deleted, and their objects cannot be deleted for 7 years."
	@cd infra && $(CDK) destroy $(STAGE1) --force

clean:
	@rm -rf infra/cdk.out infra/.pytest_cache infra/**/__pycache__ infra/__pycache__
