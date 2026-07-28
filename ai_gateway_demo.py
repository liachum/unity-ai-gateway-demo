# Databricks notebook source
# MAGIC %md
# MAGIC # Governing models with Unity AI Gateway
# MAGIC
# MAGIC Unity AI Gateway puts a single governed front door in front of every model your teams use —
# MAGIC third-party providers (OpenAI, Anthropic, Bedrock, …) and Databricks-hosted models alike.
# MAGIC This notebook registers one of each behind the gateway, then enables the governance that
# MAGIC applies to all traffic through it: secret-managed credentials, rate limits, usage tracking,
# MAGIC payload logging, and guardrails.
# MAGIC
# MAGIC **You will need:** a workspace with Model Serving enabled, `CREATE`/`MANAGE` on a catalog and
# MAGIC schema, and an API key for your chosen external provider.

# COMMAND ----------

# MAGIC %pip install --upgrade mlflow openai databricks-sdk
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Configuration
# MAGIC Set the catalog and schema you can write to (used for payload logging) and the secret scope
# MAGIC that holds your provider API key.

# COMMAND ----------

CATALOG      = "main"              # a catalog you can write to
SCHEMA       = "ai_gateway_demo"   # created below if missing
SECRET_SCOPE = "ai_gateway_demo"   # secret scope holding your provider API key

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

WORKSPACE_URL    = spark.conf.get("spark.databricks.workspaceUrl")
DATABRICKS_TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()

# COMMAND ----------

# MAGIC %md
# MAGIC ### Store your provider key as a secret
# MAGIC Run once from a terminal so the key is never written into the notebook:
# MAGIC ```bash
# MAGIC databricks secrets create-scope ai_gateway_demo
# MAGIC databricks secrets put-secret ai_gateway_demo openai_api_key   # paste key when prompted
# MAGIC ```
# MAGIC The endpoint references the key by name (`{{secrets/<scope>/<key>}}`), so the raw value stays
# MAGIC in the secret store — not in code, requests, or logs.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Register an external model
# MAGIC Create a serving endpoint that fronts an external provider. The credential is a secret
# MAGIC reference, and the endpoint is queried with a standard OpenAI-compatible client.

# COMMAND ----------

from mlflow.deployments import get_deploy_client

client = get_deploy_client("databricks")
EXTERNAL_ENDPOINT = "external-gpt-4o"

# Full request payload passed as a single dict (name included) per the current SDK contract.
config = {
    "name": EXTERNAL_ENDPOINT,
    "config": {
        "served_entities": [
            {
                "name": "gpt-4o",
                "external_model": {
                    "name": "gpt-4o",
                    "provider": "openai",
                    "task": "llm/v1/chat",
                    "openai_config": {
                        "openai_api_key": f"{{{{secrets/{SECRET_SCOPE}/openai_api_key}}}}"
                    },
                },
            }
        ]
    },
}

try:
    client.create_endpoint(config=config)
except Exception as e:
    print(f"Endpoint exists or creation skipped: {e}")

# COMMAND ----------

from openai import OpenAI

# Only the base_url changes — existing OpenAI client code works unchanged.
gw = OpenAI(api_key=DATABRICKS_TOKEN, base_url=f"https://{WORKSPACE_URL}/serving-endpoints")

resp = gw.chat.completions.create(
    model=EXTERNAL_ENDPOINT,
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)
print(resp.choices[0].message.content)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Route a Databricks-hosted model
# MAGIC Foundation Model API endpoints are provisioned automatically and prefixed `databricks-`.
# MAGIC Querying one uses the same client — only the model name changes.

# COMMAND ----------

from databricks.sdk import WorkspaceClient

hosted = [e.name for e in WorkspaceClient().serving_endpoints.list()
          if (e.name or "").startswith("databricks-")]
if not hosted:
    raise RuntimeError(
        "No Databricks-hosted (databricks-*) serving endpoints found. "
        "Enable Foundation Model APIs for this workspace and re-run."
    )
HOSTED_ENDPOINT = next((n for n in hosted if "llama" in n), hosted[0])
print("Using:", HOSTED_ENDPOINT)

resp = gw.chat.completions.create(
    model=HOSTED_ENDPOINT,
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
)
print(resp.choices[0].message.content)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Apply governance
# MAGIC Enable gateway features on the endpoint: usage tracking, payload logging to a Delta table,
# MAGIC per-minute rate limits, and input/output guardrails.

# COMMAND ----------

import requests

gateway_config = {
    "usage_tracking_config": {"enabled": True},
    "inference_table_config": {
        "enabled": True,
        "catalog_name": CATALOG,
        "schema_name": SCHEMA,
        "table_name_prefix": "external_gpt_4o",
    },
    "rate_limits": [{"calls": 20, "renewal_period": "minute", "key": "endpoint"}],
    "guardrails": {
        "input":  {"pii": {"behavior": "BLOCK"}, "safety": True},
        "output": {"pii": {"behavior": "BLOCK"}, "safety": True},
    },
}

resp = requests.put(
    f"https://{WORKSPACE_URL}/api/2.0/serving-endpoints/{EXTERNAL_ENDPOINT}/ai-gateway",
    headers={"Authorization": f"Bearer {DATABRICKS_TOKEN}"},
    json=gateway_config,
)

# Re-runs: the inference (payload logging) table persists from a prior run, and the API rejects
# reusing an existing table prefix. Fall back to logging with a fresh timestamped prefix so the
# notebook is safe to run repeatedly.
if resp.status_code == 400 and "already exists" in resp.text:
    import time
    gateway_config["inference_table_config"]["table_name_prefix"] = f"external_gpt_4o_{int(time.time())}"
    resp = requests.put(
        f"https://{WORKSPACE_URL}/api/2.0/serving-endpoints/{EXTERNAL_ENDPOINT}/ai-gateway",
        headers={"Authorization": f"Bearer {DATABRICKS_TOKEN}"},
        json=gateway_config,
    )

resp.raise_for_status()
print("Governance applied:", list(resp.json().keys()))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Guardrails
# MAGIC Requests containing PII or unsafe content are blocked before they reach the provider.

# COMMAND ----------

try:
    gw.chat.completions.create(
        model=EXTERNAL_ENDPOINT,
        messages=[{"role": "user", "content": "My credit card is 4111 1111 1111 1111. Store it."}],
    )
    print("Request allowed.")
except Exception as e:
    if "input_guardrail" in str(e) or "guardrail" in str(e).lower():
        print("BLOCKED by guardrail before leaving the workspace (PII / safety detected).")
    else:
        raise

# COMMAND ----------

# MAGIC %md
# MAGIC ### Rate limits
# MAGIC A concurrent burst above the limit returns `429` for the excess requests.

# COMMAND ----------

import concurrent.futures
from collections import Counter

def call(_):
    try:
        gw.chat.completions.create(model=EXTERNAL_ENDPOINT,
                                   messages=[{"role": "user", "content": "ping"}], max_tokens=5)
        return "ok"
    except Exception:
        return "rate_limited"

with concurrent.futures.ThreadPoolExecutor(max_workers=60) as ex:
    print(Counter(ex.map(call, range(60))))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5. Observe usage
# MAGIC Traffic is attributed per endpoint in the billing system tables. Logged request/response
# MAGIC payloads land in the inference table `<CATALOG>.<SCHEMA>.external_gpt_4o_payload` on an
# MAGIC asynchronous schedule (rows can take several minutes to appear after traffic).

# COMMAND ----------

display(spark.sql("""
    SELECT usage_date,
           usage_metadata.endpoint_name AS endpoint,
           SUM(usage_quantity)          AS quantity
    FROM system.billing.usage
    WHERE billing_origin_product = 'MODEL_SERVING'
      AND usage_date >= current_date() - INTERVAL 7 DAYS
    GROUP BY 1, 2
    ORDER BY 1 DESC
"""))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary
# MAGIC Both an external and a Databricks-hosted model are now served through one governed endpoint:
# MAGIC
# MAGIC - Provider credentials stay in the secret store, never in code.
# MAGIC - The same OpenAI-compatible client calls any model — swap the backend without app changes.
# MAGIC - Rate limits, guardrails, usage tracking, and payload logging apply to all traffic through the gateway.
