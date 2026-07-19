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
