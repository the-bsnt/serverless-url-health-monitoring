# URL Health Monitor

A serverless URL/API health monitoring system built on Azure Functions (Python, Flex Consumption plan). It periodically checks a list of URLs, records status code and response time, and exposes an HTTP endpoint that reports uptime % and latest health per URL.

---

## 1. Overview

|                   |                                                                                                       |
| ----------------- | ----------------------------------------------------------------------------------------------------- |
| **Purpose**       | Monitor uptime and response latency of a set of URLs/APIs on a schedule, without managing any servers |
| **Runtime**       | Python 3.11, Azure Functions v2 (decorator-based) programming model                                   |
| **Hosting plan**  | Azure Functions **Flex Consumption** (pay-per-execution, auto-scaling, no idle cost)                  |
| **Data store**    | Azure Table Storage (single storage account, shared with app's deployment/runtime storage)            |
| **Observability** | Application Insights (auto-provisioned with the Function App)                                         |
| **Trigger types** | Timer trigger (scheduled check) + HTTP trigger (on-demand report)                                     |

**What it does, in one sentence:** every 5 minutes a background function pings each configured URL and writes the result (status code, response time, timestamp) to a table, and a separate HTTP endpoint reads that table back to compute and display an uptime percentage per URL.

---

## 2. Architecture

```
                    ┌──────────────────────┐
                    │   Azure Timer         │
                    │   (NCRONTAB schedule) │
                    │   fires every 5 min   │
                    └──────────┬───────────┘
                               │ triggers
                               ▼
                    ┌──────────────────────┐
                    │  PingUrls function    │
                    │  (Timer Trigger)      │
                    │                        │
                    │  1. Reads TARGET_URLS │
                    │  2. HTTP GET each URL │
                    │  3. Measures latency  │
                    │  4. Writes result row │
                    │  5. Logs WARN if slow │
                    │     or failed         │
                    └──────────┬───────────┘
                               │ writes rows
                               ▼
                    ┌──────────────────────┐
                    │  Azure Table Storage  │
                    │  table: "urlhealth"   │
                    │                        │
                    │  PartitionKey = URL   │
                    │  RowKey = timestamp   │
                    │  StatusCode           │
                    │  ResponseMs           │
                    │  Success              │
                    │  Timestamp            │
                    └──────────┬───────────┘
                               │ queried by
                               ▼
                    ┌──────────────────────┐
                    │  GetReport function   │
                    │  (HTTP Trigger)        │
                    │                        │
                    │  1. Query rows/URL     │
                    │  2. Get latest status  │
                    │  3. Compute uptime %   │
                    │  4. Return JSON/text   │
                    └──────────┬───────────┘
                               │ HTTPS + function key
                               ▼
                    ┌──────────────────────┐
                    │   Caller (browser,    │
                    │   curl, dashboard)    │
                    └──────────────────────┘

     ── Application Insights (side-channel) ──
     PingUrls / GetReport logging.info() / logging.warning()
     calls stream here automatically for monitoring/alerting.
```

### Why one storage account handles everything

A single **general-purpose v2 storage account** provides three services this project uses:

| Service                                                               | Used for                                                                                                                      |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| **Blob Storage**                                                      | Holds the zipped code package deployed via OneDeploy (Flex Consumption requirement), plus internal Functions host bookkeeping |
| **Table Storage**                                                     | Stores the `urlhealth` table — the actual health-check data                                                                   |
| _(Queue Storage exists on the account but is unused by this project)_ | —                                                                                                                             |

No second storage account or Cosmos DB is needed.

---

## 3. Azure Resources

| Resource             | Type                                                 | Purpose                                                   |
| -------------------- | ---------------------------------------------------- | --------------------------------------------------------- |
| Resource Group       | `Microsoft.Resources/resourceGroups`                 | Logical container for all resources below                 |
| Storage Account      | `Microsoft.Storage/storageAccounts` (StorageV2, LRS) | Backs Blob (code package) + Table (health data)           |
| Function App         | `Microsoft.Web/sites` (Flex Consumption plan, Linux) | Hosts and runs the two functions                          |
| Application Insights | `Microsoft.Insights/components`                      | Auto-created with the Function App; collects logs/metrics |

---

## 4. Functions

### `PingUrls` — Timer Trigger

- **Schedule:** `0 */5 * * * *` (NCRONTAB: every 5 minutes)
- **Logic:**
  1. Loop over `TARGET_URLS` (hardcoded list in `function_app.py`)
  2. `GET` each URL with a 10s timeout, measure elapsed time
  3. Write one row per URL per run to the `urlhealth` table
  4. `logging.warning()` if the request failed or took > 2000ms — flows into Application Insights automatically

### `GetReport` — HTTP Trigger

- **Route:** `GET /api/report`
- **Auth level:** `FUNCTION` (requires a function key — see below)
- **Logic:**
  1. Query all rows per URL's `PartitionKey`
  2. Sort by `RowKey` (timestamp-based) to find the latest check
  3. Compute `uptime % = successful checks / total checks`
  4. Return JSON by default, or a plain-text table with `?format=text`

---

## 5. Table Schema (`urlhealth`)

| Field          | Type              | Description                                                                            |
| -------------- | ----------------- | -------------------------------------------------------------------------------------- |
| `PartitionKey` | string            | URL with scheme/slashes stripped (e.g. `www.google.com`)                               |
| `RowKey`       | string            | `yyyyMMddHHmmss` + random suffix — ensures uniqueness and rough chronological ordering |
| `Url`          | string            | Full URL that was checked                                                              |
| `StatusCode`   | int               | HTTP status code returned (`0` if the request errored out entirely)                    |
| `ResponseMs`   | int               | Response time in milliseconds                                                          |
| `Success`      | bool              | `true` if status code < 400                                                            |
| `Timestamp`    | string (ISO 8601) | UTC time of the check                                                                  |

---

## 6. Local Development

```bash
# 1. Install tooling
npm install -g azure-functions-core-tools@4 azurite

# 2. Start the local storage emulator (run from a dedicated folder)
mkdir -p ~/azurite-data && cd ~/azurite-data
azurite --silent

# 3. In the project folder — set up Python env
cd url-health-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 4. Run the app locally
func start
```

`local.settings.json` points `AzureWebJobsStorage` at `UseDevelopmentStorage=true`, so everything runs against Azurite — no real Azure resources touched during local dev. Azurite persists its state as `__azurite_db_*__.json` files and `__blobstorage__` / `__queuestorage__` folders in whatever directory you launched it from; these are local-only and should be gitignored.

To test the timer immediately instead of waiting 5 minutes, temporarily set `run_on_startup=True` on the `@app.timer_trigger(...)` decorator.

Test the report endpoint locally:

```bash
curl "http://localhost:7071/api/report?format=text"
```

---

## 7. Deployment Flow

### Prerequisites

- Azure CLI (`az --version`)
- Azure Functions Core Tools v4 (`func --version`)
- Python 3.11 matching the deployed runtime
- Logged in: `az login`

### Provisioning (one-time)

```bash
az group create --name url-health-rg --location eastus

az storage account create \
  --name <unique-storage-name> \
  --resource-group url-health-rg \
  --location eastus \
  --sku Standard_LRS

az functionapp create \
  --resource-group url-health-rg \
  --name <your-function-app-name> \
  --storage-account <unique-storage-name> \
  --flexconsumption-location eastus \
  --runtime python \
  --runtime-version 3.11 \
  --os-type Linux
```

This single `functionapp create` call also auto-provisions the linked Application Insights resource — no separate step needed.

### Deploying code

Flex Consumption does **not** support classic Kudu zip-push or portal drag-and-drop deploy. It uses a mechanism called **OneDeploy**, which both Core Tools and the VS Code extension handle transparently:

```bash
cd url-health-monitor
func azure functionapp publish <your-function-app-name>
```

Under the hood this zips the project, uploads it to the app's deployment blob container (`app-package-<app-name>-<hash>`), and the app picks it up and restarts.

**Alternative (UI-based):** VS Code → Azure Functions extension → right-click the project under **Workspace** → **Deploy to Function App...** → select the app. Same OneDeploy mechanism, no terminal required.

### Verifying deployment

```bash
# Tail live logs
func azure functionapp logstream --name <your-function-app-name>

# Get the function key for GetReport
az functionapp function keys list \
  --resource-group url-health-rg \
  --name <your-function-app-name> \
  --function-name GetReport

# Call the live endpoint
curl "https://<your-function-app-name>.azurewebsites.net/api/report?format=text&code=<function-key>"
```

---

## 8. Security Notes

- `GetReport` uses **function-level auth** (`auth_level=FUNCTION`) — callers must supply a function key via `?code=` or the `x-functions-key` header.
- Use the **function key** (scoped to `GetReport` only) for normal access — not the **master/host key**, which grants admin-level access across the whole app and should be reserved for platform tooling.
- `local.settings.json`, Azurite's `__azurite_db_*__.json` files, and `.venv/` should all be gitignored — none of them belong in source control.

---

## 9. Cost

Flex Consumption bills per execution + memory-time, with a monthly free grant. At a 5-minute check interval (~8,640 timer executions/month) plus occasional report calls, this workload comfortably stays within the free tier for a personal/learning project.

---

## 10. Possible Next Steps

- Move `TARGET_URLS` out of code and into its own Table (`urltargets`) so URLs can be added/removed without redeploying
- Add real alerting (email via SendGrid/Azure Communication Services, or Teams/Slack webhook) triggered from the `logging.warning()` paths
- Add a lightweight frontend (static web app) consuming the JSON from `GetReport` instead of raw `curl`
- Add retention/cleanup logic so `urlhealth` doesn't grow unbounded
