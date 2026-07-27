# Unity AI Gateway — hands-on demo

A single, runnable Databricks notebook that puts both a **third-party model** (e.g. OpenAI) and a
**Databricks-hosted model** behind [Unity AI Gateway](https://docs.databricks.com/aws/en/ai-gateway),
then turns on the governance that applies to all traffic through it:

- **Secret-managed credentials** — provider API keys stay in the secret store, never in code.
- **One client for every model** — the same OpenAI-compatible client calls any backend; swap models without app changes.
- **Rate limits, guardrails, usage tracking, and payload logging** — enabled with one config call.

`ai_gateway_demo.py` is a Databricks source-format notebook. Import it and run the cells top to bottom.

## Prerequisites

- A Databricks workspace with **Model Serving** enabled.
- **`CREATE` / `MANAGE`** on a catalog and schema (used to log request/response payloads).
- An **API key** for the external provider you want to register (the notebook uses OpenAI by default).
- The [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) installed and authenticated
  (`databricks auth login`) — used once to store your provider key as a secret.

## Setup

1. **Import the notebook.** In your workspace: **Workspace → Import → File**, and select `ai_gateway_demo.py`
   (or clone this repo and import it). It imports as a Python notebook.

2. **Store your provider API key as a secret** (run once from a terminal so the key is never typed into the notebook):
   ```bash
   databricks secrets create-scope ai_gateway_demo
   databricks secrets put-secret ai_gateway_demo openai_api_key   # paste your key when prompted
   ```

3. **Set the config cell.** Open the notebook and edit the three values in **section 1**:
   ```python
   CATALOG      = "main"              # a catalog you can write to
   SCHEMA       = "ai_gateway_demo"   # created automatically if missing
   SECRET_SCOPE = "ai_gateway_demo"   # the scope from step 2
   ```
   > **Important:** set `CATALOG` to a catalog you actually have write access to. `main` is not writable
   > in every workspace — if you see `PERMISSION_DENIED: Catalog 'main' is not accessible`, change it to a
   > catalog you own (ask your workspace admin if unsure).

4. **Run all cells, top to bottom.** Serverless or any general-purpose cluster works.

## What each section does

| Section | What it demonstrates |
|---|---|
| 1. Configuration | Creates the schema and reads the workspace URL + token. |
| 2. Register an external model | Creates a serving endpoint fronting OpenAI, then queries it with a standard OpenAI client. |
| 3. Route a Databricks-hosted model | Queries a Foundation Model API model with the *same* client — only the model name changes. |
| 4. Apply governance | Enables usage tracking, payload logging, rate limits, and guardrails in one call. Then shows a PII request being **blocked**, and a burst tripping the **rate limit** (`429`). |
| 5. Observe usage | Queries per-endpoint usage from the billing system tables. |

## Notes

- **Guardrails** block requests containing PII or unsafe content *before* they reach the provider. In the
  demo, a message containing a credit-card number is rejected with an `input_guardrail_triggered` error.
- **Rate limits** are enforced per minute. They only trip under a *concurrent* burst — the notebook fires
  60 parallel requests against a limit of 20 so you can see the `429`s.
- **Payload logging** to the inference table (`<CATALOG>.<SCHEMA>.external_gpt_4o_payload`) is asynchronous —
  rows can take several minutes to appear after traffic.
- To register a different provider (Anthropic, Amazon Bedrock, Google Vertex AI, Azure OpenAI, or a custom
  endpoint), change the `external_model` block in section 2. See
  [External models in Model Serving](https://docs.databricks.com/aws/en/machine-learning/foundation-models/external-models)
  for each provider's config.

## Cleanup

```python
# In a notebook cell, or via the CLI:
# databricks serving-endpoints delete external-gpt-4o
# databricks secrets delete-scope ai_gateway_demo
# DROP SCHEMA <CATALOG>.ai_gateway_demo CASCADE;
```

## References

- [AI governance with Unity AI Gateway](https://docs.databricks.com/aws/en/ai-gateway)
- [Configure AI Gateway on model serving endpoints](https://docs.databricks.com/aws/en/ai-gateway/configure-ai-gateway-endpoints)
- [External models in Model Serving](https://docs.databricks.com/aws/en/machine-learning/foundation-models/external-models)
- [Monitor model serving endpoints using inference tables](https://docs.databricks.com/aws/en/ai-gateway/inference-tables)
