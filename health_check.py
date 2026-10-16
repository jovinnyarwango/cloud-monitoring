import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import boto3

# Targets live in targets.json next to this file. Each target:
#   url (required), max_ms (slowness threshold),
#   expect_text (page must contain), expect_json_key (JSON must have key),
#   expect_json (JSON must contain these exact key/value pairs)
HERE = os.path.dirname(os.path.abspath(__file__))

SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:971089639309:cloud-alerts")
# DynamoDB table (partition key "url", string). Without it every failing run alerts, as before.
STATE_TABLE = os.environ.get("STATE_TABLE", "")
TIMEOUT_SECONDS = 5
RETRY_DELAY_SECONDS = 2
REMINDER_SECONDS = 3600
SSL_WARN_DAYS = 14

sns = boto3.client("sns")
_table = None


def load_targets():
    with open(os.path.join(HERE, "targets.json")) as f:
        return json.load(f)


def _once(target):
    """One attempt. Returns (state, detail, response_ms)."""
    url = target["url"]
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "JovinMonitor/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as response:
            elapsed_ms = int((time.time() - start) * 1000)
            body = response.read().decode("utf-8", errors="replace")

            if response.status != 200:
                return ("DOWN", f"status {response.status}", elapsed_ms)

            if "expect_text" in target and target["expect_text"] not in body:
                return ("DOWN", f"missing expected text '{target['expect_text']}'", elapsed_ms)

            if "expect_json_key" in target or "expect_json" in target:
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return ("DOWN", "response is not valid JSON", elapsed_ms)
                key = target.get("expect_json_key")
                if key is not None and (not isinstance(data, dict) or key not in data):
                    return ("DOWN", f"JSON missing key '{key}'", elapsed_ms)
                for k, v in target.get("expect_json", {}).items():
                    if not isinstance(data, dict) or data.get(k) != v:
                        return ("DOWN", f"JSON '{k}' is {data.get(k) if isinstance(data, dict) else None!r}, expected {v!r}: {body[:200]}", elapsed_ms)

            if elapsed_ms > target.get("max_ms", 5000):
                return ("DEGRADED", f"slow: {elapsed_ms}ms > {target['max_ms']}ms limit", elapsed_ms)

            return ("UP", "status 200", elapsed_ms)

    except urllib.error.HTTPError as e:
        elapsed_ms = int((time.time() - start) * 1000)
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            body = ""
        return ("DOWN", f"status {e.code}: {body}".strip(), elapsed_ms)
    except Exception as e:
        elapsed_ms = int((time.time() - start) * 1000)
        return ("DOWN", str(e), elapsed_ms)


def check_site(target):
    """Return (state, detail, response_ms). A failure is retried once before it counts."""
    result = _once(target)
    if result[0] == "DOWN":
        time.sleep(RETRY_DELAY_SECONDS)
        return _once(target)
    return result


def ssl_days_left(url):
    """Days until the site's certificate expires, or None if not https or unreadable."""
    if not url.startswith("https://"):
        return None
    host = url.split("/")[2].split(":")[0]
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT_SECONDS) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                not_after = tls.getpeercert()["notAfter"]
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        return (expires - datetime.now(timezone.utc)).days
    except Exception:
        return None


def full_check(target):
    state, detail, ms = check_site(target)
    if state == "UP":
        days = ssl_days_left(target["url"])
        if days is not None and days < SSL_WARN_DAYS:
            return ("DEGRADED", f"certificate expires in {days} days", ms)
    return (state, detail, ms)


def table():
    global _table
    if _table is None and STATE_TABLE:
        _table = boto3.resource("dynamodb").Table(STATE_TABLE)
    return _table


def decide(prev, state, now):
    """What to tell the owner. prev is the stored item or None. Returns (kind, new_item).

    kind is "alert" (just went bad), "reminder" (still bad for an hour since last alert),
    "recovered" (back up) or None (nothing to say)."""
    bad = state != "UP"
    was_bad = bool(prev) and prev.get("state") != "UP"
    if bad and not was_bad:
        return "alert", {"state": state, "since": now, "last_alert": now}
    if bad and was_bad:
        last = int(prev.get("last_alert", 0))
        if now - last >= REMINDER_SECONDS:
            return "reminder", {"state": state, "since": prev.get("since", now), "last_alert": now}
        return None, {"state": state, "since": prev.get("since", now), "last_alert": last}
    if was_bad:
        return "recovered", {"state": "UP", "since": now, "last_alert": 0}
    return None, {"state": "UP", "since": (prev or {}).get("since", now), "last_alert": 0}


def lambda_handler(event, context):
    targets = load_targets()
    results = []
    alerts, reminders, recovered = [], [], []
    now = int(time.time())
    tbl = table()

    for target in targets:
        state, detail, ms = full_check(target)
        line = f"{state} | {target['url']} | {detail} | {ms}ms"
        results.append(line)
        print(line)

        if tbl is None:
            if state != "UP":
                alerts.append(line)
            continue

        prev = tbl.get_item(Key={"url": target["url"]}).get("Item")
        kind, item = decide(prev, state, now)
        tbl.put_item(Item={"url": target["url"], **item})
        if kind == "alert":
            alerts.append(line)
        elif kind == "reminder":
            reminders.append(line)
        elif kind == "recovered":
            recovered.append(line)

    summary = "\n\nAll results:\n" + "\n".join(results)
    if alerts or reminders:
        bad = alerts + reminders
        down = sum(1 for p in bad if p.startswith("DOWN"))
        bits = []
        if down:
            bits.append(f"{down} DOWN")
        if len(bad) - down:
            bits.append(f"{len(bad) - down} DEGRADED")
        prefix = "MONITOR ALERT" if alerts else "MONITOR STILL FAILING"
        parts = []
        if alerts:
            parts.append("New problems:\n" + "\n".join(alerts))
        if reminders:
            parts.append("Still failing:\n" + "\n".join(reminders))
        if recovered:
            parts.append("Recovered:\n" + "\n".join(recovered))
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=f"{prefix}: {', '.join(bits)}",
                    Message="\n\n".join(parts) + summary)
    elif recovered:
        sns.publish(TopicArn=SNS_TOPIC_ARN, Subject=f"MONITOR RECOVERED: {len(recovered)} back up",
                    Message="Back to normal:\n" + "\n".join(recovered) + summary)

    problems = [r for r in results if not r.startswith("UP")]
    return {
        "statusCode": 200,
        "body": json.dumps({"checked": len(targets), "problems": len(problems)}),
    }
