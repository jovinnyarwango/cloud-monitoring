import json
import time
import urllib.request
import boto3

# The sites this function watches
TARGETS = [
    "https://www.google.com",
    "http://jovin-cloud-resume-2026.s3-website-us-east-1.amazonaws.com",
]

SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:971089639309:cloud-alerts"
TIMEOUT_SECONDS = 5

sns = boto3.client("sns")


def check_site(url):
    """Return (is_healthy, status_code_or_error, response_ms)."""
    start = time.time()
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_SECONDS) as response:
            elapsed_ms = int((time.time() - start) * 1000)
            return (response.status == 200, response.status, elapsed_ms)
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return (False, str(e), elapsed_ms)


def lambda_handler(event, context):
    """Entry point — AWS calls this on every invocation."""
    results = []
    failures = []

    for url in TARGETS:
        healthy, status, ms = check_site(url)
        line = f"{'UP' if healthy else 'DOWN'} | {url} | {status} | {ms}ms"
        results.append(line)
        print(line)  # goes to CloudWatch Logs
        if not healthy:
            failures.append(line)

    if failures:
        sns.publish(
            TopicArn=SNS_TOPIC_ARN,
            Subject=f"HEALTH CHECK ALERT: {len(failures)} site(s) down",
            Message="Failed checks:\n" + "\n".join(failures)
                    + "\n\nAll results:\n" + "\n".join(results),
        )

    return {
        "statusCode": 200,
        "body": json.dumps({"checked": len(TARGETS), "failures": len(failures)}),
    }
