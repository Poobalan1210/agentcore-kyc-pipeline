"""Did the prompt and model changes actually reach the deployed harnesses?"""
import json

import boto3

cfn = boto3.client("cloudformation", region_name="us-east-1")
cc = boto3.client("cloudcontrol", region_name="us-east-1")
h = {o["OutputKey"]: o["OutputValue"] for o in
     cfn.describe_stacks(StackName="KycHarnesses")["Stacks"][0]["Outputs"]}

for key, out in (("documents", "HarnessDocumentsArn"), ("fraud", "HarnessFraudArn"),
                 ("identity", "HarnessIdentityArn")):
    p = json.loads(cc.get_resource(
        TypeName="AWS::BedrockAgentCore::Harness",
        Identifier=h[out])["ResourceDescription"]["Properties"])
    prompt = json.dumps(p.get("SystemPrompt"))
    print("=" * 74)
    print(key)
    print("  model        :", p["Model"]["BedrockModelConfig"]["ModelId"])
    print("  MaxIterations:", p.get("MaxIterations"))
    print("  AllowedTools :", p.get("AllowedTools"))
    print("  says 'submit tool':", "submit tool" in prompt)
    print("  says 'reply IS the report':", "reply IS the report" in prompt)
    print("  tail:", prompt[-260:])
