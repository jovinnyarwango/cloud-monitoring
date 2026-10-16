# Cloud Monitoring & Alerting

Serverless synthetic monitoring on AWS: a scheduled Lambda function
health-checks target sites every 5 minutes and alerts via SNS email
when anything is down.

## Architecture

EventBridge (rate: 5 min) -> Lambda (Python 3.12) -> checks targets
-> on failure publishes to SNS topic -> email notification.
Function output streams to CloudWatch Logs. A CloudWatch billing
alarm on the same SNS topic emails when monthly charges exceed $5.

## Components

- `health_check.py` — the Lambda: checks each target with a 5s
  timeout, logs UP/DOWN with response times, publishes alert on
  failure
- IAM: dedicated execution role with basic logging plus an inline
  least-privilege policy allowing sns:Publish to one topic only
- EventBridge rule with a resource-based Lambda permission scoped
  to that single rule

## Cost

~8,640 invocations/month against Lambda's permanent free tier of
1M — effectively $0. This monitor also watches my portfolio site,
whose live status is displayed on the site itself.

## Alerts that stay readable

- **Targets** live in `targets.json` (add a site without touching code). Checks: `max_ms`,
  `expect_text`, `expect_json_key`, and `expect_json` (exact values, e.g. `{"ok": true}`).
- A failed check is retried once after 2 seconds before it counts.
- Error alerts include the response body (StoreBook's `/api/health` says which part is unhealthy).
- Certificates expiring within 14 days show as DEGRADED.
- With a state table, you get **one email when a site goes down**, a reminder every hour while it
  stays down, and a **recovered** email. Without it, every failing run emails (the old behaviour).

### One-time setup for the state table

```bash
aws dynamodb create-table --table-name monitor-state \
  --attribute-definitions AttributeName=url,AttributeType=S \
  --key-schema AttributeName=url,KeyType=HASH --billing-mode PAY_PER_REQUEST

aws lambda update-function-configuration --function-name YOUR_FUNCTION \
  --environment "Variables={STATE_TABLE=monitor-state}"
```

Add to the Lambda role: `dynamodb:GetItem` and `dynamodb:PutItem` on the `monitor-state` table.
Package `health_check.py` **and** `targets.json` in the deployment zip.

### Dead-man's switch (alerts if the monitor itself stops)

```bash
aws cloudwatch put-metric-alarm --alarm-name monitor-not-running \
  --namespace AWS/Lambda --metric-name Invocations \
  --dimensions Name=FunctionName,Value=YOUR_FUNCTION \
  --statistic Sum --period 900 --evaluation-periods 1 --threshold 1 \
  --comparison-operator LessThanThreshold --treat-missing-data breaching \
  --alarm-actions arn:aws:sns:us-east-1:971089639309:cloud-alerts
```

### Tests

`python3 -m unittest discover -s tests`
