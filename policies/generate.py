#!/usr/bin/env python3
"""Stage-1 -> stage-2 bridge — task 8.1.

Reads the merged CDK outputs from stage 1 (`cdk deploy <all stage-1 stacks> --outputs-file ...`)
and writes one `.cedar` file per jurisdiction per policy kind under `policies/generated/`
(gitignored — these are derived from a specific account's live ARNs and would go stale the
moment anyone else deployed).

This has to be a separate step from `cdk synth`/`cdk deploy` for `KycPolicies` because Cedar
policies name concrete Gateway ARNs and Cedar forbids wildcard resources (F-17): the Gateway
must exist and be described before its ARN can be written into a policy, so no amount of CDK
token wiring inside one `cdk deploy` can substitute for actually running stage 1 first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cedar import JURISDICTION_CODES, all_statements  # noqa: E402

#: Every agent key that has at least one tool (design §5.1). Briefing and Notify are deliberately
#: excluded — they have no tools and so need no authorisation policy.
_TOOL_BEARING_AGENTS = (
    "documents", "identity", "fraud", "prescreen", "compliance")


def _merged_outputs(path: Path) -> dict[str, str]:
    """Flatten `{stackName: {outputKey: value}}` into one `{outputKey: value}` map.

    Stage 1 is several stacks (`KycFoundation`, `KycGateways`, ...); an output name is unique
    across them in practice (each stack's outputs are stack-specific), so flattening is safe and
    much easier for the rest of this module to consume than re-deriving which stack owns which
    Gateway or role ARN.
    """
    raw = json.loads(path.read_text())
    merged: dict[str, str] = {}
    for stack_outputs in raw.values():
        merged.update(stack_outputs)
    return merged


def _harness_roles(outputs: dict[str, str]) -> dict[str, str]:
    """agent key -> role ARN, from the `HarnessRole<Suffix>Arn` outputs `foundation_stack.py`
    emits. Keyed by the agent's lowercase key, matching `AGENT_BY_KEY` in `kyc/config.py`."""
    roles: dict[str, str] = {}
    for key, value in outputs.items():
        if key.startswith("HarnessRole") and key.endswith("Arn"):
            agent_key = key[len("HarnessRole"):-len("Arn")].lower()
            roles[agent_key] = value
    missing = [a for a in _TOOL_BEARING_AGENTS if a not in roles]
    if missing:
        raise KeyError(
            f"missing HarnessRole*Arn outputs for {missing} — was KycFoundation deployed "
            f"and included in --outputs-file?")
    return {k: v for k, v in roles.items() if k in _TOOL_BEARING_AGENTS}


def _gateway_arn(outputs: dict[str, str], code: str) -> str:
    key = f"Gateway{code.upper()}Arn"
    try:
        return outputs[key]
    except KeyError as exc:
        raise KeyError(
            f"missing {key} — was KycGateways deployed and included in --outputs-file?"
        ) from exc


def generate(*, outputs_path: Path, out_dir: Path) -> list[Path]:
    """Writes two kinds of output, both under `out_dir` (gitignored):

    * One `.cedar` file per statement, per jurisdiction — `<code>/<StatementName>.cedar` — for
      a human to read or diff a single statement without parsing JSON.
    * `manifest.json` — `{code: [{name, cedar, kind, engineArn}, ...]}` — what `KycPolicies`
      (the CDK stack, task 8.1) actually reads. **One `AWS::BedrockAgentCore::Policy` resource
      per statement**, confirmed live: the API rejects a `Statement` containing more than one
      top-level `permit`/`forbid`/`suppressOutput` block (see `cedar.py`'s module docstring).
    """
    outputs = _merged_outputs(outputs_path)
    harness_roles = _harness_roles(outputs)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    manifest: dict[str, list[dict[str, str]]] = {}

    for code in JURISDICTION_CODES:
        gateway_arn = _gateway_arn(outputs, code)
        engine_arn = outputs[f"PolicyEngine{code.upper()}Arn"]
        statements = all_statements(gateway_arn=gateway_arn, harness_roles=harness_roles)

        code_dir = out_dir / code
        code_dir.mkdir(parents=True, exist_ok=True)
        entries = []
        for statement in statements:
            path = code_dir / f"{statement.name}.cedar"
            path.write_text(statement.cedar + "\n")
            written.append(path)
            entries.append({
                "name": statement.name,
                "cedar": statement.cedar,
                "kind": statement.kind,
                "gatewayArn": gateway_arn,
                "policyEngineArn": engine_arn,
            })
        manifest[code] = entries

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    written.append(manifest_path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", required=True, type=Path,
                        help="Merged stage-1 cdk-outputs.json")
    parser.add_argument("--out", required=True, type=Path,
                        help="Directory to write generated .cedar files into")
    args = parser.parse_args()

    if not args.outputs.exists():
        print(f"error: {args.outputs} does not exist — run stage 1 first", file=sys.stderr)
        return 1

    written = generate(outputs_path=args.outputs, out_dir=args.out)
    print(f"wrote {len(written)} Cedar policy files to {args.out}/")
    for path in written:
        print(f"  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
