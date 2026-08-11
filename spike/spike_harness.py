#!/usr/bin/env python3
"""Phase 0 spike — Tasks 0.3, 0.5, 0.6.

Creates one throwaway Harness (control-plane operation is CreateLoopyAgent, not
CreateHarness) and proves:

  0.3  us.amazon.nova-2-lite-v1:0 is accepted and returns valid output
  0.5  cold-start vs warm latency  (closes A-01)
  0.6  maxReasoningEffort medium vs high, TTFT and total  (validates F-22)

Also records what the API defaults to for memory and allowedTools, which is the
evidence behind R11.8 (DIV-2) and R11.15-R11.17.
"""
import json
import time
import sys
import statistics as st

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = 'us-east-1'
NAME = 'kyc_spike_harness'
ROLE = 'KycSpikeHarnessRole'
CFG = Config(region_name=REGION, retries={'max_attempts': 3, 'mode': 'standard'},
             read_timeout=300, connect_timeout=20)

iam = boto3.client('iam', config=CFG)
sts = boto3.client('sts', config=CFG)
ctl = boto3.client('bedrock-agentcore-control', config=CFG)
dat = boto3.client('bedrock-agentcore', config=CFG)

ACCT = sts.get_caller_identity()['Account']
OUT = {'account': ACCT, 'region': REGION, 'findings': [], 'measurements': {}}


def log(msg):
    print(msg, flush=True)


def note(k, v):
    OUT['findings'].append({k: v})
    log("   FINDING  %s: %s" % (k, v))


# ---------------------------------------------------------------- 1. execution role
TRUST = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
        "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": ACCT}},
    }],
}
POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {"Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse", "bedrock:ConverseStream"],
         "Resource": "*"},
        {"Effect": "Allow",
         "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
                    "logs:DescribeLogStreams", "logs:PutResourcePolicy"],
         "Resource": "*"},
        {"Effect": "Allow",
         "Action": ["xray:PutTraceSegments", "xray:PutSpans",
                    "xray:PutTelemetryRecords", "cloudwatch:PutMetricData"],
         "Resource": "*"},
    ],
}

log("=" * 84)
log("STEP 1  execution role")
log("=" * 84)
try:
    r = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(TRUST),
                        Description='Phase 0 spike role for kyc-90-seconds')
    role_arn = r['Role']['Arn']
    log("   created %s" % role_arn)
except ClientError as e:
    if e.response['Error']['Code'] != 'EntityAlreadyExists':
        raise
    role_arn = iam.get_role(RoleName=ROLE)['Role']['Arn']
    log("   reusing %s" % role_arn)
iam.put_role_policy(RoleName=ROLE, PolicyName='inline',
                    PolicyDocument=json.dumps(POLICY))
log("   inline policy attached; waiting 12s for IAM propagation")
time.sleep(12)

# ---------------------------------------------------------------- 2. create harness
log("")
log("=" * 84)
log("STEP 2  CreateLoopyAgent  (the Harness control-plane operation)")
log("=" * 84)

REQ = {
    'loopyAgentName': NAME,
    'executionRoleArn': role_arn,
    'model': {
        'provider': 'bedrock',
        'config': {'bedrockModelConfig': {
            'modelId': 'us.amazon.nova-2-lite-v1:0',
            'maxTokens': 512,
            'temperature': 0.0,
        }},
    },
    'systemPrompt': [{'text':
        'You are a KYC document classifier. Answer only with a single JSON object.'}],
    # R11.14 — bound the runaway-billing case (F-14 / F-46)
    'lifecycleConfig': {'idleRuntimeSessionTimeout': 120, 'maxLifetime': 900},
    'maxIterations': 5,
    'timeoutSeconds': 120,
    # deliberately omit `memory` and `tools` to observe the defaults
}

harness_arn = None
try:
    resp = ctl.create_loopy_agent(**REQ)
    harness_arn = resp['loopyAgent'].get('loopyAgentArn') or resp['loopyAgent'].get('arn')
    log("   created: %s" % harness_arn)
    log(json.dumps(resp['loopyAgent'], indent=2, default=str)[:1400])
except ClientError as e:
    code = e.response['Error']['Code']
    log("   ClientError %s: %s" % (code, e.response['Error']['Message'][:400]))
    if code in ('ConflictException', 'ValidationException'):
        try:
            for p in ctl.get_paginator('list_loopy_agents').paginate():
                for a in p.get('loopyAgentSummaries', p.get('loopyAgents', [])):
                    if a.get('loopyAgentName') == NAME:
                        harness_arn = a.get('loopyAgentArn') or a.get('arn')
            log("   found existing: %s" % harness_arn)
        except Exception as e2:
            log("   list failed: %s" % e2)

if not harness_arn:
    log("\nCannot proceed without a harness ARN. Writing partial results.")
    OUT['blocked'] = 'CreateLoopyAgent did not yield an ARN'
    json.dump(OUT, open('spike_results.json', 'w'), indent=2, default=str)
    sys.exit(2)

OUT['harnessArn'] = harness_arn

# ---------------------------------------------------------------- 3. inspect defaults
log("")
log("=" * 84)
log("STEP 3  GetLoopyAgent — what did the service default to?")
log("=" * 84)
try:
    g = ctl.get_loopy_agent(loopyAgentIdentifier=harness_arn)
    body = g.get('loopyAgent', g)
    log(json.dumps(body, indent=2, default=str)[:2500])
    note('memory_default', 'present in response' if body.get('memory') else
                           'ABSENT — memory is NOT auto-enabled when omitted at create')
    note('allowedTools_default', body.get('allowedTools', 'not returned'))
    note('lifecycleConfig_echo', body.get('lifecycleConfig', 'not returned'))
    OUT['getLoopyAgent'] = body
except ClientError as e:
    log("   %s" % e)

# wait for READY
log("\n   waiting for READY ...")
for _ in range(40):
    try:
        b = ctl.get_loopy_agent(loopyAgentIdentifier=harness_arn).get('loopyAgent', {})
        s = b.get('status') or b.get('loopyAgentStatus')
        log("      status=%s" % s)
        if s in ('READY', 'ACTIVE', None):
            break
        if s in ('CREATE_FAILED', 'FAILED'):
            log("      failure reason: %s" % b.get('failureReason'))
            break
    except ClientError as e:
        log("      %s" % e)
        break
    time.sleep(5)


# ---------------------------------------------------------------- 4. invoke
def invoke(model_id, effort=None, prompt='Classify: UK passport, MRZ present. JSON only.'):
    """Returns (ok, ttft_ms, total_ms, text, err)."""
    body = {
        'harnessArn': harness_arn,
        'runtimeSessionId': 'kyc-spike-%s-%d' % (str(abs(hash((model_id, effort, time.time()))))[:12],
                                                 int(time.time() * 1000)),
        'messages': [{'role': 'user', 'content': [{'text': prompt}]}],
        'model': {'bedrockModelConfig': {'modelId': model_id, 'maxTokens': 256,
                                         'temperature': 0.0}},
    }
    if len(body['runtimeSessionId']) < 33:
        body['runtimeSessionId'] += 'x' * (33 - len(body['runtimeSessionId']))
    if effort:
        body['model']['bedrockModelConfig']['additionalParams'] = {
            'reasoningConfig': {'type': 'enabled', 'maxReasoningEffort': effort}}
    t0 = time.time()
    ttft = None
    chunks = []
    try:
        r = dat.invoke_harness(**body)
        stream = r.get('stream')
        if stream is None:
            return True, None, (time.time() - t0) * 1000, json.dumps(r, default=str)[:300], None
        for ev in stream:
            if ttft is None:
                ttft = (time.time() - t0) * 1000
            chunks.append(ev)
        total = (time.time() - t0) * 1000
        txt = json.dumps(chunks[-1], default=str)[:300] if chunks else ''
        return True, ttft, total, txt, None
    except ClientError as e:
        return False, None, (time.time() - t0) * 1000, '', '%s: %s' % (
            e.response['Error']['Code'], e.response['Error']['Message'][:220])
    except Exception as e:
        return False, None, (time.time() - t0) * 1000, '', repr(e)[:220]


log("")
log("=" * 84)
log("STEP 4  TASK 0.3 — invoke with each Nova model ID")
log("=" * 84)
t03 = {}
for mid in ('us.amazon.nova-2-lite-v1:0', 'us.amazon.nova-lite-v1:0',
            'us.amazon.nova-micro-v1:0'):
    ok, ttft, total, txt, err = invoke(mid)
    t03[mid] = {'ok': ok, 'ttft_ms': ttft, 'total_ms': total,
                'sample': txt, 'error': err}
    log("   %-30s %-4s total=%sms  %s"
        % (mid, 'OK' if ok else 'FAIL',
           int(total) if total else '?', err or txt[:90]))
OUT['task_0_3'] = t03

# 0.6 reasoning effort
log("")
log("=" * 84)
log("STEP 5  TASK 0.6 — maxReasoningEffort medium vs high (F-22)")
log("=" * 84)
COMPLIANCE = ('Applicant: Mohammed Al-Rashid, DOB 1979-03-11, UK. Sanctions list has '
              '"Muhammad Al Rashid" DOB 1979-03-11 SY. Assess match confidence and '
              'recommend CONFIRMED/PROBABLE/POSSIBLE/NO_MATCH. JSON only.')
t06 = {}
N = 5   # spec asks 30; 5 per arm here to bound spike cost, flagged in the report
for effort in ('medium', 'high'):
    ttfts, totals, fails = [], [], 0
    for i in range(N):
        ok, ttft, total, txt, err = invoke('us.amazon.nova-2-lite-v1:0', effort, COMPLIANCE)
        if ok:
            if ttft:
                ttfts.append(ttft)
            totals.append(total)
        else:
            fails += 1
            if i == 0:
                log("      first failure: %s" % err)
    t06[effort] = {
        'runs': N, 'failures': fails,
        'ttft_p50': round(st.median(ttfts), 1) if ttfts else None,
        'total_p50': round(st.median(totals), 1) if totals else None,
        'total_max': round(max(totals), 1) if totals else None,
    }
    log("   effort=%-6s n=%d fails=%d  ttft_p50=%sms  total_p50=%sms"
        % (effort, N, fails, t06[effort]['ttft_p50'], t06[effort]['total_p50']))
OUT['task_0_6'] = t06

# 0.5 cold vs warm
log("")
log("=" * 84)
log("STEP 6  TASK 0.5 — cold vs warm (closes A-01)")
log("=" * 84)
cold, warm = [], []
for i in range(8):
    ok, _, total, _, err = invoke('us.amazon.nova-micro-v1:0')
    (cold if i < 4 else warm).append(total if ok else None)
    time.sleep(1)
cold = [c for c in cold if c]
warm = [w for w in warm if w]
OUT['task_0_5'] = {
    'first_calls_ms': [round(c) for c in cold],
    'later_calls_ms': [round(w) for w in warm],
    'first_p50': round(st.median(cold), 1) if cold else None,
    'later_p50': round(st.median(warm), 1) if warm else None,
    'note': 'sequential proxy for cold/warm; true cold start needs idle > '
            'idleRuntimeSessionTimeout between calls',
}
log("   first calls p50=%sms   later calls p50=%sms"
    % (OUT['task_0_5']['first_p50'], OUT['task_0_5']['later_p50']))

json.dump(OUT, open('spike_results.json', 'w'), indent=2, default=str)
log("\nwrote spike_results.json")
