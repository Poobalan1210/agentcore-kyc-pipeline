#!/usr/bin/env python3
"""Diagnose why CreateLoopyAgent returns 'Unable to determine service/operation name'."""
import boto3
import json
from botocore.config import Config
from botocore.exceptions import ClientError

R = 'us-east-1'
CFG = Config(region_name=R, retries={'max_attempts': 2})
ctl = boto3.client('bedrock-agentcore-control', config=CFG)
dat = boto3.client('bedrock-agentcore', config=CFG)

print("control endpoint :", ctl.meta.endpoint_url)
print("data endpoint    :", dat.meta.endpoint_url)
print("signing name     :", ctl.meta.service_model.signing_name)
print("api version      :", ctl.meta.service_model.api_version)

m = ctl.meta.service_model
for op in ('CreateLoopyAgent', 'ListLoopyAgents', 'GetLoopyAgent', 'ListGateways',
           'ListAgentRuntimes', 'ListMemories', 'ListPolicyEngines'):
    try:
        o = m.operation_model(op)
        print("  %-22s %s %s" % (op, o.http['method'], o.http['requestUri']))
    except Exception as e:
        print("  %-22s NOT IN MODEL (%s)" % (op, e))

print("\n--- live calls, cheapest first ---")


def call(label, fn, **kw):
    try:
        r = fn(**kw)
        keys = list(r.keys())
        print("  %-24s OK    keys=%s" % (label, [k for k in keys if k != 'ResponseMetadata']))
        return r
    except ClientError as e:
        err = e.response['Error']
        print("  %-24s %-24s %s" % (label, err['Code'], err['Message'][:150]))
    except Exception as e:
        print("  %-24s EXC  %s" % (label, repr(e)[:150]))
    return None


call('ListGateways', ctl.list_gateways)
call('ListAgentRuntimes', ctl.list_agent_runtimes)
call('ListPolicyEngines', ctl.list_policy_engines)
call('ListMemories', ctl.list_memories)
call('ListLoopyAgents', getattr(ctl, 'list_loopy_agents', lambda **k: {}))
call('ListBrowsers', ctl.list_browsers)
call('ListCodeInterpreters', ctl.list_code_interpreters)

print("\n--- what operations exist with 'Loopy' ---")
print(sorted(o for o in m.operation_names if 'Loopy' in o))
print("\n--- paths of all Create* ops (looking for a /harnesses path) ---")
for op in sorted(m.operation_names):
    o = m.operation_model(op)
    uri = o.http['requestUri']
    if 'harness' in uri.lower() or 'loopy' in uri.lower():
        print("  %-26s %s %s" % (op, o.http['method'], uri))
