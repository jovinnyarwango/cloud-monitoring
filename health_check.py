import json
import time
import urllib.request
import boto3

# Each target: url (required), max_ms (slowness threshold),
# expect_text (page must contain), expect_json_key (JSON must have key)
TARGETS = [
    {
        "url": "https://www.google.com",
        "max_ms": 2000,
    },
    {
        "url": "https://d3vtcypf6er9fb.cloudfront.net",
        "max_ms": 2000,
        "expect_text": "Jovin",
    },
    {
        "url": "http://jovin-cloud-resume-2026.s3-website-us-east-1.amazonaws.com",
        "max_ms": 3000,
        "expect_text": "Jovin",
    },
    {
        "url": "https://q9bh2nlt22.execute-api.us-east-1.amazonaws.com/Prod/resume",
        "max_ms": 3000,
        "expect_json_key": "resume",
    },
]

SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:971089639309:cloud-alerts"
TIMEOUT_SECONDS = 5

sns = boto3.client("sns")


def check_site(target):
    """Return (state, detail, response_ms) where state is UP/DEGRADED/DOWN."""
    url = target["url"]
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JovinMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            elapsed_ms = int((time.time() - start) * 1000)
            body = response.read().decode("utf-8", errors="replace")

            if response.status != 200:
                return ("DOWN", f"status {response.status}", elapsed_ms)

            # Content verification: expected text present?
            if "expect_text" in target and target["expect_text"] not in body:
                return ("DOWN", f"missing expected text '{target['expect_text']}'", elapsed_ms)

            # Function verification: valid JSON with expected key?
            if "expect_json_key" in target:
                try:
                    data = json.loads(body)
                    if target["expect_json_key"] not in data:
                        return ("DOWN", f"JSON missing key '{target['expect_json_key']}'", elapsed_ms)
                except json.JSONDecodeError:
                    return ("DOWN", "response is not valid JSON", elapsed_ms)

            # Latency check: answered correctly, but slow?
            if elapsed_ms > target.get("max_ms", 5000):
                return ("DEGRADED", f"slow: {elapsed_ms}ms > {target['max_ms']}ms limit", elapsed_ms)

            return ("UP", "status 200", elapsed_ms)

    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return ("DOWN", str(e), elapsed_ms)


def lambda_handler(event, context):
    results = []
    problems = []

    for target in TARGETS:
        state, detail, ms = check_site(target)
        line = f"{state} | {target['url']} | {detail} | {ms}ms"
        results.append(line)
        print(line)
        if state != "UP":
            problems.append(line)

    if problems:
        down = sum(1 for p in problems if p.startswith("DOWN"))
        degraded = len(problems) - down
        subject_bits = []
        if down:
            subject_bits.append(f"{down} DOWN")
        if degraded:
            subject_bits.append(f"{degraded} DEGRADED")
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"MONITOR ALERT: {', '.join(subject_bits)}",
            Message="Problems:\n" + "\n".join(problems)
                    + "\n\nAll results:\n" + "\n".join(results),
        )

    return {
        "statusCode": 200,
        "body": json.dumps({"checked": len(TARGETS), "problems": len(problems)}),
    }
