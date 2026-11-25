# Live Azure validation

Initial deployment validated on **20 November 2026**; both demos retested on **25 November 2026** using real Azure services and synthetic data, not local demo output.

## 25 November 2026 retest

| Demo | Actual execution | Result |
|---|---|---|
| Multi-file PII | Ran `scripts.anonymize_files` on all eight shared `.txt` files with Azure Language PII | Eight successful outputs, zero failed files; all 16 known identifier canaries absent across names, addresses, email, phone, cards, SSN and IBAN |
| Content preservation | Inspected generated files for expected technical facts | HTTP 504 and circuit-breaker content retained |
| RAG | Ran `scripts.check_azure` against the deployed API, real AI Search indexes, Foundry IQ KBs and prompt agents | Index privacy, technical relevance, collection scoping, multi-turn conversation, saved-answer reveal/hide and remote history-canary checks passed |
| Browser | Ran live browser acceptance, then navigated the user's signed-in page | Real original/index comparison, PII-visible quotation, reveal/hide, summary and policy collection passed |

Standalone outputs: ignored `.data\pii-run-20261125`. RAG summary: ignored `.data\azure-test\results.json`. The RAG retest reused the existing indexed corpus rather than claiming a fresh ingestion/deployment on 25 November; managed ingestion was previously exercised on 20 November.

In the signed-in browser, a PII-visible answer contained **11 protected occurrences**. Reveal displayed the fictional name/email/address/phone/test card; hide returned the same answer to opaque tokens while the protected inspector remained unchanged. A subsequent technical summary contained zero placeholders and correctly disabled reveal. Switching to policies reset the conversation and returned relevant guidance with two evidence excerpts.

No mocked Azure responses or request interception were used for these live acceptance runs. These are finite synthetic checks, not proof of universal PII removal or preserved answer quality for arbitrary documents. Presidio was not live-tested. The known SDK `created_by` serializer warning during history inspection remained non-blocking.

### Guided question masking: 25 November UI update

The deployed demo now separates **fictional PII question buttons** from technical-only
suggestions. The inspector compares the user's as-typed question with the protected question
submitted to the agent, highlighting category masks.

An unmocked browser run clicked and submitted the first two new support examples:

- The name/email question showed the original fictional values only in the as-typed UI
  preview; the agent-input preview contained redaction markers and none of those values.
- The test-card question removed the card before agent invocation. The actual IQ/source
  quotation returned opaque PII tokens; reveal restored the fictional test card in the
  displayed answer while the protected answer panel remained unchanged.

Both completed without UI errors using real Azure PII, AI Search, IQ and the prompt agent.
The targeted application suite passed 12 tests, including a check of the actual argument
passed from the backend to the agent after question redaction. These unit checks supplement,
not replace, the live-browser validation.

For the reproducible deployment entry point and diagrams, use [the README](README.md#deploy-demo-2-to-azure); for the UI, open <https://app-pii-rag-b5a65d.azurewebsites.net>.

## 20 November 2026: deployment and expanded-corpus validation

There is no external-party integration in either demo.

### Demo 1: standalone file processing

`scripts.anonymize_files` successfully called **Azure Language PII** on all **eight physical input files**, and separately on an explicit two-file list. It wrote de-identified `.txt` files and a manifest, without a token vault or restoration keys.

Across the eight-file export, **0 of 16 known identifier canaries remained**:

| Category | Known canaries | Remaining |
|---|---:|---:|
| Names | 3 | 0 |
| Emails | 3 | 0 |
| Street addresses | 3 | 0 |
| Phone numbers | 3 | 0 |
| Credit cards | 2 | 0 |
| Government ID | 1 | 0 |
| IBAN | 1 | 0 |

Technical facts including HTTP 504 and circuit-breaker remediation remained. Final outputs are in ignored `.data\demo1-azure-output`; the list-input run is in `.data\demo1-azure-selected-final`. Inputs are unchanged. The separate test manifest with original canary values was not exported. Windows CRLF preservation was also checked.

### Demo 2: RAG with the same files

The reusable `scripts.deploy_azure` command successfully redeployed the live API, configured private connectivity and permissions, preserved existing secrets/mappings, and provisioned the real AI Search/Foundry objects. This was an idempotent deployment test against the existing isolated stack, not a second fresh-stack rollout; region quotas and all fresh-resource creation branches are not certified by that rerun.

The same eight sample files were then uploaded through the API. Real managed indexers completed with **zero errors and zero warnings**. Validation required every file's expected technical fact to be present in indexed content, not merely a minimum document count.

The resulting indexes contain **11 support chunks and 4 policy chunks**, including prior synthetic test uploads; there are eight current fixture files, not fifteen unique fixture documents.

Live checks passed for:

- All seven PII categories above absent from indexed fields.
- Checkout summaries and follow-ups, plus relevant deduplication and timeout/circuit-breaker answers for the new shipping and verification cases.
- Separate collection retrieval, exact-token backend restoration, and pseudonymized Foundry conversation items.

**Local validation:** 37 tests passed; Ruff, Pyright and JavaScript syntax checks passed. Presidio remains an alternative adapter with contract tests; these live results used Azure PII.

The deployment script also exposed a Windows configuration-file replacement lock during testing. The corrected writer stages a complete configuration once, skips unchanged settings and retries only bounded permission errors; it does not regenerate existing mapping keys on a normal redeployment.

## Initial RAG validation

**Passed the Azure end-to-end flow:**

1. Authenticated upload through the deployed API.
2. Raw text written to private Blob Storage by the API managed identity.
3. Managed Search indexer reads Blob through an approved shared private link.
4. Custom Web API skill calls real Azure Language PII and commits encrypted mappings.
5. Only protected chunks are embedded and projected into separate collection indexes.
6. Foundry prompt agent calls the collection-bound Foundry IQ retrieval function.
7. Agent answers technical questions and follows up using its conversation.
8. Backend restores exact evidence-backed tokens for UI display.
9. Stored Foundry conversation items contain none of the tested clear identifier canaries.
10. After an API restart, existing token mappings still restore correctly.
11. A real browser authenticated to the hosted UI, received a live agent answer, displayed restored PII separately from the protected inspector, showed citations, and switched to an isolated policy conversation.

Both indexers completed with **zero errors and zero warnings**. That initial corpus had **five support chunks and two policy chunks**; it included a duplicate synthetic INC-101 uploaded during the initial connectivity probe, not five unique incidents. The latest expanded corpus is described above.

The repeatable check also confirmed:

- Synthetic names, emails, and the test card canary were absent from the indexed text/metadata fields.
- The incoming question's name and test-card value were removed before agent invocation.
- Support answers retained connection-pool/concurrency remediation facts.
- Policy answers retained timestamps/correlation/deployment guidance.
- Policy retrieval returned only the policy collection.
- An evidence quotation preserved exact tokens, and the backend restored them without unsupported-token warnings.

## Open the live UI

<https://app-pii-rag-b5a65d.azurewebsites.net>

The page requires the sample application access key. It is the `RAG_APP_KEY` value in the ignored local `.env`; it is intentionally not printed in this report or committed. It is a shared sample key, not end-user/document authorization.

### PII visibility and restoration fix

The reported summary behavior was reproduced live: an answer contained **zero** PII placeholders while its retrieved excerpts contained **16**. Requesting restoration did not change that technical-only answer. The former checkbox also applied only to the next submitted question.

The deployed UI now:

- Compares a bundled original fictional file with protected chunks read from **real Azure AI Search**, highlights placeholders, and displays actual category counts.
- Provides **Run a PII-visible example**, which calls the real prompt agent/Foundry IQ and asks for a quotation retaining opaque placeholders.
- Switches the **same latest answer** between protected and original display using server-held evidence. It does not submit another model turn or rewrite the index/history.
- Disables the reveal control and explains why when there are no reversible placeholders.
- Distinguishes output-scan masks, model-written redaction markers and unsupported/stale tokens from mapped, reversible placeholders.

Unmocked browser acceptance passed: real Search before/after comparison, a technical-only summary, a PII-visible generated answer, reveal then hide, unchanged `/api/chat` request count during display switching, unchanged protected inspector text, citations and login behavior. No request interception or fabricated Azure responses were used for that acceptance run. A separate direct Azure call generated 11 reversible placeholder occurrences and the backend restored all 11 with zero blocked values.

The live comparison test found that Blob extraction appends LF before hashing. The viewer now accepts the exact fixture version or its extracted trailing-LF version, uses an exact phrase query, and still rejects other document versions. This was fixed against the actual stored scope hashes, not by returning a synthetic protected-text fallback.

Twenty-six focused application/protection tests passed separately; those unit fixtures are not the basis for the live Azure claim. No network settings or enterprise MCP/LiteLLM resources were changed.

### Access-screen fix and retest

The reported screenshot showed an unauthenticated page, not an outage. Live checks returned HTTP 200 for health, HTTP 401 for anonymous configuration, and HTTP 200 with the correct application key.

The deployed UI now shows a dedicated **Access key required** screen with clear key-source instructions rather than an empty disabled workspace. It distinguishes a rejected key from service/network failures, supports retry, and never enables chat without a valid conversation. A failed reset clears the old session binding before another collection can be used.

Retested in a real browser: missing/wrong/valid keys, successful Azure chat/citations, and refresh requiring a new connection. Controlled browser fixtures additionally covered HTML 503 responses, retry, and a failed conversation reset without stale-session reuse. Six targeted API regression tests passed, including application-key versus skill-key rejection. Full Azure Search/agent/history/restoration checks passed again after deployment.

The workstation's first parallel checker run hit Azure CLI's default 10-second token-process timeout; the local checker now allows 60 seconds and passed on rerun. This was separate from the deployed app's healthy managed-identity flow.

Try:

- `Summarize the checkout incidents and their fixes.`
- `Find incidents similar to database connection exhaustion.`
- Switch to **Service policies**: `What details should we capture during checkout incidents?`
- For the restoration demonstration, enable restore and ask:
  `Quote the checkout HTTP 504 incident evidence verbatim, including its reporter sentence. Preserve every opaque placeholder token exactly; do not infer original identifiers.`

Ordinary summaries may omit incidental PII entirely, so enabling restoration need not change every answer. A quotation deliberately exercises token copying/restoration; it is not person-specific lookup.

## Deployed topology

| Resource | Name / location |
|---|---|
| Dedicated test resource group | `rg-pii-rag-e2e-b5a65d` |
| Search | `srch-pii-b5a65d`, Sweden Central, **Standard S1**, one search unit |
| API app | `app-pii-rag-b5a65d`, Sweden Central, Python 3.12 |
| App Service plan | `plan-pii-rag-b5a65d`, Linux **B1**, one instance |
| Raw Storage | `stpiiragb5a65d`, East US 2; public networking, anonymous Blob access, and shared-key access remain disabled |
| VNet | `vnet-pii-b5a65d`, Sweden Central |
| App integration subnet | `app-integration`, delegated to App Service |
| API-to-Blob endpoint | `pe-pii-blob-b5a65d`, private Blob DNS linked to the VNet |
| Search-to-Blob connection | Approved shared private link `private-blob` |
| Persistent encrypted mappings | API app `/home/data/pii-rag/mappings.sqlite3` |
| New isolated Foundry project | `pii-rag-b5a65d`, under existing account `fdy-iq-rbac-tbkj2wgzdhqi6` in `rg-foundry-iq-toolbox-rbac` |
| Reused deployments | Existing `gpt-4o-mini` and `text-embedding-3-small`; existing account's Language endpoint |
| Sample agents | `pii-support-agent` and `pii-policies-agent`, version 2 at validation |
| Per-collection Search objects | `pii-support-*` and `pii-policies-*` |

No existing model deployment or unrelated Search index was modified.

### Why the private route was necessary

The initial Basic Search creation in East US 2 encountered capacity exhaustion. Search was created in Sweden Central instead.

App Service B1 had zero quota in East US 2. A plan could be created in Sweden Central and upgraded to B1 there.

Storage's public network setting remained disabled after attempted updates; direct data-plane requests from the workstation returned `AuthorizationFailure`. We did not change organizational policies or depend on public Blob access. The working route uses App Service VNet integration and a Blob private endpoint, plus Search's separately approved managed private link. S1 was selected because private execution of a custom-skill pipeline requires that supported tier.

The existing-resource provisioning command now supports:

```powershell
.\.venv\Scripts\python -m scripts.provision --skip-storage --private-indexer
```

`--skip-storage` assumes the containers already exist (prepared through ARM in this test). Do not combine it with `--seed`: the workstation has no Blob private-network route. Seed through the deployed upload API instead.

## Repeat the live checks

The ignored local `.env` is configured for this deployment. Azure CLI login and the resource roles are required:

```powershell
.\.venv\Scripts\python -m scripts.check_azure --timeout 120
```

To append a fresh set of synthetic fixtures through the deployed API:

```powershell
.\.venv\Scripts\python -m scripts.check_azure --seed --timeout 600
```

`--seed` intentionally appends new random-named blobs; it is not an idempotent replacement operation. Do not repeatedly seed when only validating an existing corpus.

The check deletes the conversations it creates. Its non-secret result summary is written to ignored `.data\azure-test\results.json`. The separate browser check verified the UI and restoration after restarting the API.

## Cost and resources left running

**The deployment is left running for inspection.** Public USD retail rates queried from the Azure Retail Prices API on the validation date:

- Search Standard S1 in Sweden Central: **$0.336/hour** for one unit.
- Linux App Service B1 in Sweden Central: **$0.018/hour** for one instance.
- Combined base compute: **$0.354/hour**, approximately **$8.50/day** or **$258.42 per 730-hour month**.
- Private endpoints, private DNS, Storage, network traffic, PII requests, model tokens, and any chargeable retrieval/semantic usage are additional. Actual subscription pricing and taxes can differ.

These are not confidential-compute SKUs. Stopping only the web app does **not** eliminate the Search or App Service plan charges.

Pricing source: [Azure Retail Prices API](https://prices.azure.com/api/retail/prices). The Search meter uses service name `Azure Cognitive Search`, product `Azure AI Search`, SKU `Standard S1` (not `Standard S1 CC`).

## Cleanup when finished

The commands below are **not executed automatically**. They destroy this test deployment and its synthetic raw data and mapping store. Review names before running.

First remove the two account-scoped role assignments made for the test managed identities. Then remove only the new Foundry project and the dedicated resource group. Keep the existing Foundry account and its existing model deployments.

```powershell
$group = "rg-pii-rag-e2e-b5a65d"
$foundryGroup = "rg-foundry-iq-toolbox-rbac"
$foundryName = "fdy-iq-rbac-tbkj2wgzdhqi6"
$foundryId = az cognitiveservices account show -g $foundryGroup -n $foundryName --query id -o tsv
$appIdentity = az webapp identity show -g $group -n app-pii-rag-b5a65d --query principalId -o tsv
$searchIdentity = az search service show -g $group -n srch-pii-b5a65d --query identity.principalId -o tsv
az role assignment delete --assignee-object-id $appIdentity --role "Cognitive Services User" --scope $foundryId
az role assignment delete --assignee-object-id $searchIdentity --role "Cognitive Services OpenAI User" --scope $foundryId
az cognitiveservices account project delete -g $foundryGroup -n $foundryName --project-name pii-rag-b5a65d
az group delete -n $group --yes
```

After cleanup, local `.env` points to removed services. Remove/update that ignored configuration or set `RAG_MODE=demo` to use the local fixture mode. Securely retire local secret copies and backups according to your retention requirements.

## Limits of this validation

- This proves the exercised Azure flow, not universal PII detection or legal anonymization.
- It is a small synthetic relevance check, not a statistically meaningful raw-versus-protected retrieval benchmark.
- Azure PII also classified generic role labels such as "reporter" and "team" as `PersonType`; these were tokenized. The tested technical facts survived, but category policy tuning should be evaluated before using a domain corpus.
- Identity-specific lookup, document RBAC, per-entity reveal permissions, PDF/OCR, source-deletion handling, and distributed application state remain out of scope.
- Presidio's adapter has mocked contract tests, but Presidio was **not** part of this Azure run.
- The installed OpenAI SDK emitted a serializer warning about the service's `created_by` metadata shape during history inspection. The conversation read and canary checks completed successfully; this does not establish that arbitrary model output cannot contain PII.
- Restored values are kept out of the application's submitted agent history. A final leakage scan still cannot retroactively remove an unexpected generated value that Foundry already stored.
