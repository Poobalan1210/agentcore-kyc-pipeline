"""Upload the synthetic document fixture the pipeline reads — closes the gap the live run exposed.

**These objects are permanent.** The evidence buckets are Object Lock COMPLIANCE for 2555 days
(F-29, set at creation and unretrofittable), so nothing written here can be deleted for seven
years — not by us, not by the root account. That is why this uploads a small, deliberately named,
stable fixture rather than throwaway test data, and why the content is entirely synthetic (R17.7).

The document tool (`s3_get_document`) does a `head_object` and returns bucket, key, size and
content type; it does not parse the image. So a small valid PNG plus a JSON sidecar carrying the
field values is sufficient for the document path, and it keeps the fixture honest — no real
passport image exists anywhere in this system.
"""

import base64
import json

import boto3

REGION = "us-east-1"
ACCT = "633898796092"
JURISDICTIONS = ("us", "uk", "au", "jp")

s3 = boto3.client("s3", region_name=REGION)

# Smallest valid PNG: 1x1, opaque. Stands in for a document scan. Deliberately not a picture of
# anything — a synthetic identity must not come with a real-looking artefact that could be reused.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

FIELDS = {
    "us": {"documentType": "PASSPORT", "issuingCountry": "US", "documentNumber": "SYN-US-000001",
           "surname": "SYNTHETIC", "givenNames": "ALEX", "dateOfBirth": "1990-04-12",
           "expiryDate": "2032-04-11", "mrzValid": True, "confidence": 0.96},
    "uk": {"documentType": "PASSPORT", "issuingCountry": "GB", "documentNumber": "SYN-GB-000001",
           "surname": "SYNTHETIC", "givenNames": "JORDAN", "dateOfBirth": "1988-11-03",
           "expiryDate": "2031-10-30", "mrzValid": True, "confidence": 0.95},
    "au": {"documentType": "PASSPORT", "issuingCountry": "AU", "documentNumber": "SYN-AU-000001",
           "surname": "SYNTHETIC", "givenNames": "RILEY", "dateOfBirth": "1992-02-20",
           "expiryDate": "2033-02-19", "mrzValid": True, "confidence": 0.94},
    "jp": {"documentType": "PASSPORT", "issuingCountry": "JP", "documentNumber": "SYN-JP-000001",
           "surname": "SYNTHETIC", "givenNames": "KAI", "dateOfBirth": "1991-07-07",
           "expiryDate": "2032-07-06", "mrzValid": True, "confidence": 0.95},
}

print("=" * 78)
print("SYNTHETIC DOCUMENT FIXTURE — PERMANENT under Object Lock COMPLIANCE")
print("=" * 78)
written = []
for code in JURISDICTIONS:
    bucket = f"kyc90-evidence-{code}-{ACCT}"
    doc_key = f"synthetic/passport-{code}-001.png"
    meta_key = f"synthetic/passport-{code}-001.fields.json"

    for key, body, ctype in (
        (doc_key, PNG, "image/png"),
        (meta_key, json.dumps(FIELDS[code], indent=2, sort_keys=True).encode(),
         "application/json"),
    ):
        # Always writes. Object Lock forbids deleting or overwriting a *version*, but a new
        # version is permitted and `head_object` reads the latest — which is how a fixture can be
        # corrected in an append-only bucket. Every prior version stays for the full 2555 days,
        # so re-running this is cheap but not free: keep the field set stable.
        # Fields go into S3 user metadata, because that is what `s3_get_document` returns from
        # the `head_object` it already performs. A sidecar JSON object is not read by anything —
        # the first version of this fixture wrote one, and the Document agent correctly reported
        # every field "not available", having been handed only a size and a content type.
        meta = {"synthetic": "true", "fixture": "kyc90-demo", "contains-pii": "false"}
        if key.endswith(".png"):
            meta.update({k.lower(): str(v) for k, v in FIELDS[code].items()})
        s3.put_object(
            Bucket=bucket, Key=key, Body=body, ContentType=ctype, Metadata=meta,
        )
        print("  %-46s uploaded %d bytes" % (f"{code}:{key}", len(body)))
        written.append({"bucket": bucket, "key": key, "action": "uploaded"})

print()
print("=" * 78)
print("VERIFY READABLE")
print("=" * 78)
ok = True
for code in JURISDICTIONS:
    bucket = f"kyc90-evidence-{code}-{ACCT}"
    key = f"synthetic/passport-{code}-001.png"
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
        print("  %-10s %-42s %d bytes  %s" % (code, key, head["ContentLength"],
                                              head.get("ContentType")))
    except Exception as exc:  # noqa: BLE001
        ok = False
        print("  %-10s %s FAILED %s" % (code, key, str(exc)[:80]))

with open("spike/seed_synthetic_docs.json", "w") as fh:
    json.dump({"objects": written, "fields": FIELDS, "readable": ok}, fh, indent=2, default=str)
print("\nwrote spike/seed_synthetic_docs.json")
print("RESULT:", "all four jurisdictions have a readable document" if ok else "SOME FAILED")
