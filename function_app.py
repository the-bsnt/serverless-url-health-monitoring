import azure.functions as func
import datetime
import json
import logging
import os
import time
import uuid

import requests
from azure.data.tables import TableServiceClient, TableEntity
from azure.core.exceptions import ResourceExistsError

app = func.FunctionApp()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STORAGE_CONNECTION_STRING = os.environ["AzureWebJobsStorage"]
TABLE_NAME = "urlhealth"
SLOW_THRESHOLD_MS = 2000

# URLs to monitor. For v1, a plain list is fine. Later you could move this
# into its own Table (e.g. "urltargets") if you want to add/remove URLs
# without redeploying.
TARGET_URLS = [
    "https://www.google.com",
    "https://www.github.com",
    "https://www.stackoverflow.com",
]


# ---------------------------------------------------------------------------
# Helpers (this is the closest Python gets to the "Dependency Injection"
# idea from the project card - C# Functions inject a TableClient into the
# function constructor, Python just builds one via a shared helper)
# ---------------------------------------------------------------------------
def get_table_client():
    service = TableServiceClient.from_connection_string(STORAGE_CONNECTION_STRING)
    try:
        service.create_table(TABLE_NAME)
    except ResourceExistsError:
        pass
    return service.get_table_client(TABLE_NAME)


def ping_url(url: str) -> dict:
    start = time.perf_counter()
    try:
        resp = requests.get(url, timeout=10)
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        return {
            "status_code": resp.status_code,
            "response_ms": elapsed_ms,
            "success": resp.status_code < 400,
        }
    except requests.RequestException as exc:
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        return {
            "status_code": 0,
            "response_ms": elapsed_ms,
            "success": False,
            "error": str(exc),
        }


def url_to_partition_key(url: str) -> str:
    # Table Storage partition keys can't contain /, \, #, ?
    return url.replace("https://", "").replace("http://", "").replace("/", "_")


# ---------------------------------------------------------------------------
# Timer Trigger - runs every 5 minutes, pings each URL, stores the result
# Cron format here is NCRONTAB: {second} {minute} {hour} {day} {month} {day-of-week}
# ---------------------------------------------------------------------------
@app.function_name(name="PingUrls")
@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False)
def ping_urls(timer: func.TimerRequest) -> None:
    logging.info("PingUrls: starting health check run")
    table_client = get_table_client()

    for url in TARGET_URLS:
        result = ping_url(url)
        now = datetime.datetime.now(datetime.timezone.utc)

        entity = TableEntity()
        entity["PartitionKey"] = url_to_partition_key(url)
        # RowKey descending-friendly-ish and unique even for same-second checks
        entity["RowKey"] = now.strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        entity["Url"] = url
        entity["StatusCode"] = result["status_code"]
        entity["ResponseMs"] = result["response_ms"]
        entity["Success"] = result["success"]
        entity["Timestamp"] = now.isoformat()

        table_client.create_entity(entity)

        if not result["success"]:
            logging.warning(f"ALERT: {url} failed (status {result['status_code']})")
        elif result["response_ms"] > SLOW_THRESHOLD_MS:
            logging.warning(f"ALERT: {url} slow response ({result['response_ms']}ms)")
        else:
            logging.info(
                f"{url} OK - {result['status_code']} in {result['response_ms']}ms"
            )

    logging.info("PingUrls: run complete")


# ---------------------------------------------------------------------------
# HTTP Trigger - GET /api/report, returns latest status + uptime % per URL
# Add ?format=text for the plain-table view, otherwise returns JSON.
# ---------------------------------------------------------------------------
@app.function_name(name="GetReport")
@app.route(route="report", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def get_report(req: func.HttpRequest) -> func.HttpResponse:
    table_client = get_table_client()

    report = []
    for url in TARGET_URLS:
        pk = url_to_partition_key(url)
        entities = list(table_client.query_entities(f"PartitionKey eq '{pk}'"))
        entities.sort(key=lambda e: e["RowKey"], reverse=True)

        if not entities:
            report.append(
                {
                    "url": url,
                    "status_code": None,
                    "response_ms": None,
                    "uptime_pct": None,
                    "checks": 0,
                }
            )
            continue

        latest = entities[0]
        success_count = sum(1 for e in entities if e.get("Success"))
        uptime_pct = round((success_count / len(entities)) * 100, 2)

        report.append(
            {
                "url": url,
                "status_code": latest["StatusCode"],
                "response_ms": latest["ResponseMs"],
                "uptime_pct": uptime_pct,
                "checks": len(entities),
            }
        )

    if req.params.get("format") == "text":
        lines = [f"{'URL':<28}{'Status':<10}{'Response':<12}{'Uptime':<8}"]
        lines.append("-" * 58)
        for r in report:
            status = str(r["status_code"]) if r["status_code"] is not None else "N/A"
            resp = f"{r['response_ms']}ms" if r["response_ms"] is not None else "Failed"
            uptime = f"{r['uptime_pct']}%" if r["uptime_pct"] is not None else "N/A"
            lines.append(f"{r['url']:<28}{status:<10}{resp:<12}{uptime:<8}")
        return func.HttpResponse("\n".join(lines), mimetype="text/plain")

    return func.HttpResponse(json.dumps(report, indent=2), mimetype="application/json")
