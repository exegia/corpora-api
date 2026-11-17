# Corpora API

FastAPI and FastMCP interfaces for [corpora-core](https://github.com/exegia/corpora-core). Text checks call the Rust project's supported `corpora-text-check` executable. Conversion requests call the core project's TEI, EPUB, and PDF converter packages and return their source-preserving bundles.

## What is exposed

| REST operation | MCP tool | Core operation |
| --- | --- | --- |
| `POST /v1/checks/structure` | `check_xml_structure` | Strict XML check and source-bound diagnostics |
| `POST /v1/checks/tei-metadata` | `check_tei_purpose` | TEI date and primary identifier policy |
| `POST /v1/language/detect` | `detect_text_language` | Language evidence and dictionary selection |
| `POST /v1/checks/spelling` | `check_text_spelling` | Optional installed-dictionary spelling report |
| `POST /v1/checks/text-fabric` | `check_text_fabric_archive` | Bounded static WARP preflight for a TF ZIP |
| `POST /v1/validate` | — | User-authenticated archive validation with real loaders or CTEI/C-USX profile checks |
| `POST /v1/ingest` | — | User-authenticated document ingestion to a schema-validated Context Fabric v1 graph |
| `GET /v1/storage`, `GET /v1/storage/{filename}`, `GET /v1/storage/{filename}/download`, `GET /v1/storage/{filename}/manifest`, `GET /v1/storage/{filename}/index`, `GET /v1/storage/{filename}/sections`, `GET /v1/storage/{filename}/content`, `GET /v1/storage/{filename}/nodes/{node}` | — | User-authenticated archive metadata, bytes, manifest, Text-Fabric structure, passages, and node detail |
| `POST /v1/storage`, `DELETE /v1/storage/{filename}` | — | Publish an owned, validated `/convert` artifact and delete a stored archive |

| Conversion endpoint | Result |
| --- | --- |
| `POST /v1/conversions/tei-to-tf` | TEI source, Text-Fabric features, manifest and checksums |
| `POST /v1/conversions/epub-to-tf` | EPUB source, assets, Text-Fabric features and manifest |
| `POST /v1/conversions/pdf-to-tf` | Score-gated PDF extraction, TEI, Text-Fabric and review report |

`GET /v1/health` reports API process health. `GET /v1/capabilities` publicly reports whether a service token is required and whether the configured Hub backend has a separate write credential. `GET /v1/openapi.json` is the exact schema used by the [Fumapress docs](docs/README.md); the API tools are mounted at `/v1/mcp/`. The existing `api.exegia.co` service keeps its routes outside `/v1/*`.

The MCP server also advertises a `text_fabric_checker` prompt for a host agent. Upload a ZIP containing one `.tf` root to the REST route, or pass its base64 bytes to the MCP tool. The checker reports an archive SHA-256, slot/node counts and located errors in `otype.tf`, `oslots.tf` and `otext.tf`. It rejects unsafe paths, links, duplicate names, archives above 3 MB, over 3,000 files or over 64 MB unpacked. A `passed` result is a **static WARP preflight only**; use a real Text-Fabric/Context-Fabric load and source comparison for full validation. The local `text-fabric-validator` skill is not bundled in this service because its redistribution license is unspecified.

The CLI's structural operation uses the core default XML options. TEI-specific `StructuralOptions` from the Rust library are not available through that CLI, so this API does not claim to perform a full TEI structural validation. A nonblocking report does not establish schema validity or conversion success. The service never applies suggestions to source text.

The spelling response keeps its existing flat `start` and `end` fields. The API validates those Unicode scalar offsets with the generated `corpora-schema-types` `TextRange` model from the same pinned core commit used by `Dockerfile.vercel`. Update the Git revision in `pyproject.toml` and `CORPORA_CORE_REF` together when adopting a new core contract.

`POST /v1/validate` accepts a multipart `file` upload named `.corpus`, `.zip`, `.ctei`, or `.cusx` and requires a verified Supabase user access token. The response uses the legacy `corpus`, `path`, `valid`, `stats`, and `reasons` fields; `path` is the uploaded filename, never a server path. `checks` is a named map with `null` for non-applicable shipped-cache checks. CTEI reports `source_verified: false` because archive validation does not prove fidelity to an external source. Invalid supported archives return HTTP 200 with `valid: false`; unsupported formats return 415 and uploads over 3 MB return 413. `.corpus` and raw TF `.zip` data run real Text-Fabric and Context-Fabric loads, including mmap cache reload. CTEI and C-USX use their package profiles. The validator route does not accept legacy `path`, loaded `corpus`, or conversion `job_id` targets; the storage publish route separately accepts only an owner-checked, succeeded conversion job and revalidates its stored archive.

`POST /v1/ingest` accepts one multipart `file` plus optional `name`, `language` (BCP 47, default `und`), and `corpus_id` (UUID). UTF-8 `.txt` is the first hosted format: its exact decoded text is preserved in one fragment under a document root and block node. PDF, DOCX, PPTX, HTML, and Markdown require an optional Docling Python worker; without it, those uploads return 503. Unsupported formats return 415, invalid inputs 422, and uploads over 3 MB return 413. Ingestion finishes and its graph is stored before the 202 response; `GET /v1/ingest/{job_id}` polls the saved job and `GET /v1/ingest/{job_id}/download` returns `graph.json`. The result is capped at 4 MB. All three routes require a verified Supabase user token; another user's job returns 404.

Ingestion uses the private `conversion_jobs` PostgREST table and result bucket. Set `SUPABASE_URL` or `PROJECT_REF`, `SUPABASE_INGEST_API_KEY` (a publishable gateway key), and `SUPABASE_JOBS_BUCKET=conversion-jobs`. The API forwards the verified signed-in user's token to Supabase only during the request; row-level policies restrict jobs, retry reservations, and graph objects to their owner. Graphs are stored at `conversion-jobs/{user_id}/{job_id}.graph.json`. A user can write their own allowed objects and rows directly, so stored graphs are revalidated when read. Each ingestion request checks that the user session remains active; the jobs, reservation, and Storage policies also enforce that check. If the session check is unavailable the API returns 503. A session revoked during an already-running operation may take effect only on a subsequent database statement. A token expiring during an operation can be refreshed and the request retried. The optional Docling worker runs in a separate Python environment with this API package and `corpora-admin[docling]` installed; point `CORPORA_INGEST_WORKER_PYTHON` to that interpreter. Worker output is revalidated against the pinned Context Fabric v1 JSON Schemas from `corpora-schema-types` before storage.

Clients can send `Idempotency-Key` with an ingestion request and reuse it for an exact retry; keys are scoped to the signed-in user, must contain 1–128 visible ASCII characters, and return 409 when reused with different input. This requires the `corpora_ingest_requests` migration and owner-scoped reservation SELECT/INSERT policies. Use a new key for a new operation; an absent key creates a new job on each request.

Reservations are retained indefinitely for this release so an old retry cannot silently create a duplicate job. They contain the owner, hashes, job ID, and creation time, not the source document or raw key. There is no background cleanup or automatic retry: abandoned reservations remain until the same user retries the same file, filename, options, and key. If only the reservation exists, that retry runs ingestion again; if a valid graph was stored but its job row was not, it finishes the job from that graph. An invalid or inaccessible stored graph fails closed and requires investigation. Reservations and abandoned graph objects consume storage, so monitor their growth; do not delete reservations without a separate retention policy that accounts for late retries and in-flight work.

Storage uses `STORAGE_BACKEND=huggingface` by default, matching the current legacy `/storage` deployment. Set `HF_STORAGE_REPO`, `HF_STORAGE_REPO_TYPE` (`bucket`, `model`, `dataset`, or `space`), and `HF_TOKEN` for private reads. Hub publishing and deletion require a separate, write-scoped `HF_STORAGE_WRITE_TOKEN`; the read token is never used for writes. Publishing uses a conditional Hub commit to prevent concurrent overwrites. Hub buckets remain readable but do not support publishing because their upload API has no equivalent conditional commit. `STORAGE_BACKEND=supabase` uses the existing private `SUPABASE_STORAGE_BUCKET` with `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_URL` or `PROJECT_REF`. A verified Supabase user JWT is required on every storage route. Publishing accepts only a completed `/convert` `job_id` owned by the caller; the API fetches and revalidates its private stored archive. It never accepts a server path. Hub archives are shared, while Supabase objects are addressed only at `{verified-user-id}/{filename}`. A delete is audit-logged with the verified user ID. List and inspect return the legacy `filename`, `size_bytes`, `repo_id`, and `url` fields, and download streams bytes without staging on the API server. Manifest, index, section, content, node, and version-history reads fetch only the verified owner's archive, read it with the packaged Context-Fabric cache, and keep no cross-request cache. Missing configuration returns 503, a missing archive or reference returns 404, corrupt archives return 422, and an upstream storage failure returns 502.

The `corpora_corpus_metadata` migration and Python registry implement the
chosen per-corpus model: Supabase stores metadata, ownership, a unique Hub
repository identifier and revision; the configured Hub organization owns the
actual corpus files. The server generates a stable repository identifier from a
caller-stable corpus UUID. Records begin in `provisioning`; this component does
not create a Hub repository, prove that it is private, or mark it ready by itself.
The creation routes described below require a verified live session. Browser roles
have no direct table access. `CORPORA_SUPERADMIN_USER_ID` is a server-controlled
user UUID, never a user-editable profile field. Review the migration, session
enforcement, Hub provisioning and namespace before enabling this registry; the
API does not apply its migrations.
The internal registry can update title, description, language and structured
properties with a revision check; properties are replaced as one object. It
does not expose these edits through an API or enforce a live session itself.
Properties are user-editable metadata and must not determine authorization.
An unwired session-bound registry uses the same database transaction for the
session lock and metadata reservation, updates, or reads. A private, fixed-SQL
session guard runs with the Supabase `postgres` role's existing session-table
privileges. The API role receives only permission to call the guard, not to
read or change sessions. It must verify JWT claims before invoking the guard;
the function cannot authenticate supplied claims. The metadata and provision
write boundaries still require separate review and grants. These adapters do
not apply migrations. A lost commit response can leave the caller
uncertain whether an update committed, so callers must reload before retrying.
Provisioning admission and outcome recording use private fixed-SQL functions:
the API gets execute permission but no direct provisioning-operation or bootstrap
write grant. Owner access is checked against the metadata, while superadmin
identities come from a private database table managed by the database administrator,
not user-editable claims. Before enabling it, configure that table consistently
with `CORPORA_SUPERADMIN_USER_ID`. These functions trust the server's verified
claims and Hub attestation; they are not accessible to browser roles.

`POST /v1/corpora` accepts `{"id":"<client-generated UUID>","title":"My corpus"}`
with a Supabase user bearer token. Keep the same UUID and title when retrying.
The API atomically reserves owner-bound metadata and claims one creation attempt,
then creates a private dataset at the server-selected organization and verifies
its identity, privacy and observed commit before recording the bootstrap receipt.
A confirmed first creation returns 201; a completed retry returns 200; a pending
attempt returns 202. Reusing the UUID with different input or another owner returns
409. `GET /v1/corpora/{id}` returns the current state to its owner or configured
superadmin; other users receive 404. Responses use `Cache-Control: no-store`.

Enable only after applying the metadata, content-version, restricted-session,
restricted-provisioning and `corpus_creation_rpc` migrations. Set
`CORPORA_CORPUS_CREATION_ENABLED=1`, `HF_CORPUS_ORGANIZATION`, a dedicated
server-only `HF_CORPUS_PROVISION_TOKEN`, `SUPABASE_SERVICE_ROLE_KEY`, and
`SUPABASE_URL` or `PROJECT_REF`, plus the normal JWT issuer/JWKS configuration.
The public RPC wrappers are executable only by `service_role`; privileged SQL
stays in the private session-guard schema. JWT signature verification happens in
the API before any claims reach these functions.

New requests after logout are rejected. A creation admitted before logout may
finish after it. A lost admission response never authorizes another Hub call.
Ambiguous remote results return 503 and remain pending or
`needs_reconciliation`; check the GET route and investigate the exact operation
instead of automatically retrying creation or adopting an existing repository.
There is no background retry or automatic reconciliation. `repository_created`
means the private repository and bootstrap receipt exist; it does not mean that
corpus content has been uploaded or validated, or that the corpus is public.
The database metadata remains `provisioning` until a validated initial import
completes. Later sensitive operations must recheck Hub privacy.

The opt-in `tests/api/test_corpus_creation_supabase.py` test runs against an empty
disposable local Supabase stack (`CORPORA_TEST_LOCAL_SUPABASE=1` with `API_URL`,
`DB_URL`, `ANON_KEY` and `SERVICE_ROLE_KEY`). It uses real Auth, PostgREST and SQL
permissions with simulated Hub calls, and removes its fixture schema and users.

After applying `corpus_metadata_api`, `GET /v1/corpora` lists the signed-in
owner's summaries in corpus-UUID order. Use `limit` (1–100) and the returned
`next_cursor` as `after` for pagination. A server-configured superadmin may
select another `owner_id`; other users cannot enumerate that owner's records.
`GET /v1/corpora/{id}/metadata` returns the title, description, language,
properties, revision and timestamps without exposing the backing repository.
`PATCH /v1/corpora/{id}/metadata` accepts `expected_revision` and the fields to
change. A stale revision returns 409, a foreign/missing corpus returns 404,
and an invalid edit returns 422. Properties replace the complete JSON object;
description and language may be cleared with null. All three routes verify the
live session in the database and use `Cache-Control: no-store`.
The original creation title is retained independently of editable metadata, so
an identical creation retry still resolves after a title edit. Creation-status
responses report that original title; the metadata route returns the current one.
After applying `corpus_initial_import`, an owner or configured superadmin can
upload initial content with `POST /v1/corpora/{id}/imports`. Send multipart fields
`operation_id` (a new client UUID), `expected_revision` (the current metadata
revision), and `file` (a `.corpus` archive, at most 3 MB). The archive must pass
the real core validator and contain one Text-Fabric root with one retained
Context-Fabric `.cfr` cache. Original files and their bytes are preserved; managed
storage paths `.corpora`, `.corpora-staging`, and `.gitattributes` are reserved.
The API checks the live session before validation and again when admitting the
operation. Only a corpus with completed repository creation and no content head
can be imported. An operation already admitted before logout may finish.

The import worker stages the archive in the private repository, persists its
publication claim, uploads content to a retained operation branch, and verifies
the downloaded Git tree and every file (including LFS payloads). Only then does
one database transaction record version 1, advance the head, and mark metadata
`ready`. This does not make the corpus public or expose its Hugging Face repository.
A confirmed first import returns 201. An identical completed retry returns 200;
an existing in-flight or uncertain operation returns 202 without dispatching
another worker. Changed input or revision for the same UUID returns 409.
`GET /v1/corpora/{id}/imports/{operation_id}` reads the durable status for the
owner or superadmin; foreign or missing operations return 404. Responses are
`Cache-Control: no-store` and never contain storage credentials or repository IDs.

There are two local worker slots and a 120-second worker deadline. Lost admission
acknowledgements, capacity exhaustion, and uncertain worker outcomes return 503;
check the operation status using the same UUID. Git branch creation and commit
publication are not automatically redispatched. The SDK may retry content-addressed
LFS transfers. An uncertain operation blocks a new import and needs reconciliation;
there is no automatic recovery or replacement import. Editing existing content
and restoring versions are separate work.

After applying `corpus_access_lifecycle`, `PATCH /v1/corpora/{id}/visibility`
accepts `expected_revision` and `visibility` (`private` or `public`) from an owner
or configured superadmin. Publishing requires ready content and a verified private
Hub commit. It changes API access only: the backing Hugging Face repository stays
private. A real change advances the metadata revision; a stale revision returns
409. `GET /v1/corpora/{id}/metadata` and `/visibility` permit anonymous reads of a
public ready corpus. Private, missing and deleted corpora return 404 to anonymous
or foreign readers. A supplied invalid or revoked token returns 401 even for a
public corpus. Owner listing, writes and operation receipts still require a live
session. These responses use `Cache-Control: no-store`; making a corpus private
again takes effect on subsequent reads. A read already admitted may finish.

`DELETE /v1/corpora/{id}` accepts a JSON body with `operation_id` (a client UUID)
and `expected_revision`. Admission rejects active or uncertain provisioning/import
operations with 409. It atomically hides the corpus before sending one bounded
deletion request to its verified private Hub repository. A successful Hub deletion
response followed by an authenticated 404 completes the receipt and returns 200.
Version and operation history is retained, and the corpus UUID/repository identity
cannot be reused. This removes the stored repository; it does not erase audit history.
The owner or superadmin may inspect `GET /v1/corpora/{id}/deletions/{operation_id}`
after normal reads return 404. An exact retry returns the receipt (200 completed,
202 executing/uncertain) without another Hub DELETE. Lost acknowledgements return
503; inspect the same receipt. An inaccessible repository before deletion is not
treated as proof of deletion. Unconfirmed outcomes stay hidden and require
reconciliation. Already-admitted deletion may finish after logout; new requests
from that revoked session are denied. Each Hub lifecycle operation has a 20-second
deadline and a 64 KiB per-response bound.

The five corpus-detail views through the new corpus identity remain a separate
integration step; existing `/v1/storage/...` reads use their existing backing store.

The content schema records content write operations, immutable-commit
versions and a current head, distinct from the metadata revision counter. An
unwired adapter admits an owner or superadmin save inside a live-session
transaction, before any Hub work. It expects a trusted hash of validated input;
it does not perform validation or Hub writes. The migration grants the service
role read access only, so admission needs a separately reviewed write boundary.
An unwired database finalizer checks the exact claim and atomically records a
trusted candidate, advances the head and sets initial readiness. The caller
must independently verify the Hub commit, parent, input and retained ref;
constructing an attestation does not verify it. The initial-import route above
provides that verification for version 1; the general save, restore and
reconciliation adapters remain unwired. Recorded commits must remain reachable
through their retained Hub references; the schema does not verify remote bytes
or enforce readiness in every possible write path. A first version has no prior
logical corpus version, but must use a separately recorded, immutable Hub
bootstrap baseline as its Git parent. This baseline is a pinned commit observed
after confirmed private repository creation, not proof of the provider's
original creation commit. A provisioning operation binds that
bootstrap to one immutable claim and must complete atomically with it; an
uncertain operation cannot be automatically reclaimed. An unwired adapter
checks a live session and owner or server-configured superadmin before
atomically admitting and claiming one attempt. A creation already underway may
finish after sign-out, including starting its Hub request; a new creation request
requires an active session. An internal dispatcher calls the bounded worker once
after a successful admission. After a confirmed creation and a second matching
private-repository observation, it records the observed baseline against the
exact claim. A lost database acknowledgement is checked against the immutable
result; a lost Hub creation response is never automatically retried. A restricted
production privilege boundary and outcome resolution remain unresolved.
An internal read-only reconciliation inspector can report whether the expected
private Hub repository is currently visible, mismatched or inconclusive. Its
observation neither proves creation provenance nor establishes worker termination;
it never adopts, retries or finalizes the operation.
The Hub observation uses a 15-second overall HTTP deadline, including response
headers and a body limited to 64 KiB. Timeout or cleanup failure is inconclusive;
the dispatcher retains the claim for reconciliation. Each observation owns its
async HTTP client and allows up to 0.1 seconds each for response and client cleanup.
This assumes nonblocking client construction and cooperative HTTPX cancellation;
it does not bound the surrounding database calls or the entire dispatcher.
An unwired finalizer can record a bootstrap from a trusted assertion, but no
component verifies that assertion; save claims fail closed when it is missing.
Before recording a new content version, a future worker must replace and verify the complete
intended tree at the resulting pinned commit, check content bytes, and guard
against branch changes. Replacing files does not erase prior Git history.
Hub organization administrators are trusted administrators and can still change
visibility, move references or delete repositories; reads must verify the
identity and privacy appropriate to the operation, and availability cannot be
guaranteed by a commit hash alone. An unwired one-shot claim
adapter lets one trusted dispatcher claim an admitted operation; it does not
run a worker or automatically reclaim an uncertain or stalled operation.
Admission receipts do not themselves authorize dispatch. A lost admission or
claim commit response must not trigger Hub work from the failed response; the
durable state needs reconciliation. A newly authenticated session for the same
actor can reattach to an identical operation; the originally admitted session
remains recorded. Receipt state is a snapshot, not an execution claim. The
finalizer checks the exact claim ID and operation state, but a database claim
cannot cancel or fence a remote Hub request that is already running.
An unwired archive inventory hashes every regular entry in the original ZIP,
including metadata and caches, with bounded full-stream decompression. It is
not corpus validation or durable staging. An unwired preparation adapter binds
the same immutable bytes and canonical archive format to the claimed digest,
inventory and real-loader
report; it requires the Text-Fabric and Context-Fabric checks to pass. It accepts
`.corpus` and `.zip` inputs; profile-only `.ctei` and `.cusx` checks are not
treated as Text-Fabric loading. Neither adapter publishes a version. The inventory
currently accepts stored and DEFLATE ZIP entries and rejects ZIP64 entry sizes
and other compression methods; expanding that support remains required before
wiring it for archives using those methods.
An unwired Hub staging adapter creates a separate operation-specific branch,
writes the validated input archive and reads it back by immutable commit SHA
with a bounded stream and digest check. An unwired recorder can persist the
trusted receipt for the exact claim; the database requires a receipt before a
version can be finalized. The recorder does not verify Hub contents itself,
and a saved receipt does not guarantee future privacy or availability. An
unwired recovery read can load an executing receipt, recheck the repository and
ref, and revalidate the bytes at its pinned commit. It does not renew a worker
claim or authorize redispatch; the claim and head can change after the read.
Remote
calls lack an end-to-end deadline, and interrupted work requires reconciliation.
A separate read-only observation can inspect a deterministic staging ref and
verify its bytes even when no receipt was recorded. A missing ref or a matching
observation does not establish whether the earlier worker is still running and
does not authorize a retry, receipt insertion or publication.
A dedicated staging state, safe dispatcher, cleanup policy and Hub parent
verification are still required.
An unwired one-shot publication claim now follows a staged receipt and is
separate from the staging claim; versions must carry that publication claim.
The claim is coordination only: the existing database role can read it, so
staging and publishing are not isolated until a restricted dispatcher or
separate production credentials are installed. It does not fence an existing
remote request or authorize automatic takeover after an uncertain outcome.
An unwired Git-object verifier checks the commit's single direct parent, exact
tree paths and regular-file modes, and verifies each blob and resolved LFS file
against trusted content hashes. Paths use exact case-sensitive Git names; this
does not establish checkout compatibility on every filesystem. It requires an
explicit decision on repository-managed files such as `.gitattributes`, uses
Git's SHA-1 object identifiers, and supports canonical three-line LFS pointers
only. The caller must verify every returned entry against the matching trusted
path, size and hash. Object acquisition, Hub privacy and ref checks, aggregate download limits,
and the publication worker still need to be implemented.
The configured Hub organization is part of each immutable repository link.
Changing it will make existing records fail closed. A namespace transfer needs
a separately reviewed Hub transfer and metadata migration or a trusted
multi-namespace transition; this component does not perform either operation.
The Hub provisioning adapter uses the server-only
`HF_CORPUS_PROVISION_TOKEN`, verifies the configured namespace is an actual Hub
organization, requests a private repository, and checks its returned identity
and privacy. The receipt includes the observed Git head, but this observation
does not establish the initial commit or authorize recording a bootstrap: other
writers may have changed the repository. The creation route adds durable one-shot
admission and an independent observation before finalization. It does not adopt an existing name
or retry an ambiguous write.
An unwired POSIX worker wrapper applies a request deadline, bounds worker
output, and accepts a trusted live-session lease. If the request times out,
an acquired lease stays held until the worker stops while its database
connection remains alive; the Hub write may still have been accepted and needs
reconciliation. If that connection is lost, PostgreSQL releases its lock and
the worker can still send a Hub write after logout, which is allowed for a request
already accepted while signed in. Abrupt application-process death and the
hosted worker environment need deployment-specific verification, and a remote write may remain in
flight after local termination. The creation API uses the one-shot dispatcher;
content upload and readiness transitions remain separate work. Privacy must also be checked at later sensitive operations; one successful observation does not keep
a repository private.

An unwired corpus-file reader checks owner-bound metadata, a ready state, a
specific Hub commit and point-in-time private visibility before streaming a
bounded file. It uses server-only `HF_CORPUS_READ_TOKEN` and does not forward
that token to redirected CDN hosts. It does not establish corpus readiness,
authenticate sessions, guarantee continuous privacy or provide a total request
deadline. Routes need a reviewed start-time session admission check and a trusted
revision before enabling it. The hosted runtime is unverified.

An additional unwired async transfer adapter applies one deadline across the
metadata lookup, Hub verification, file transfer and sink backpressure. It uses
a dedicated HTTP client for each transfer and allows a short cleanup period
after the deadline. Client setup is synchronous and must not block; its
transport and sink must cooperate with cancellation. Partial bytes already
delivered cannot be recalled. It does not integrate a live-session guard,
authorize revisions, or replace the existing reader.

Authenticated `/v1/ai/drafts` endpoints create, list, inspect and export private
working drafts from a caller-owned completed conversion. They use the existing
`corpora_ai_storage` Supabase RPC and private `corpus-ai-drafts` bucket from
the legacy `corpora-py` draft migrations. Archives are staged under immutable
owner/document/revision keys and checked against their SHA-256 digest before
publication and download. The API requires the server-side Supabase service key;
no caller-supplied storage path is accepted.
The source-pinned legacy database setup is in `supabase/migrations/`; apply it
only after reviewing it against the target database and verifying its existing
tables and policies. The API does not apply migrations automatically.


Authenticated `/v1/ai/providers` lists the legacy provider guidance without
accepting API keys. `/v1/ai/validate` validates a version-pinned scope against
an accessible stored corpus or owned completed conversion, returning located
findings and rejecting stale version or content hashes.
For conversion jobs, the API validates the downloaded archive again before
using it; a saved result from an earlier validation is not sufficient.
Corpus detail, reference, and scope reads run the native loader in a separate
short-lived process per request so its state cannot cross server worker threads.
Results exceeding the worker's 3.9 MB encoded JSON limit return 413 rather than
being truncated; request a smaller page where the route supports pagination.


The authenticated `/v1/refs` endpoints create and resolve versioned
Text-Fabric references and compact tokens from stored corpora. Resolution
rejects stale versions and ambiguous compact corpus names; access follows the
configured storage backend's rules.

## Local development

Requirements: Python 3.13+, uv, Git for the pinned core schema package, Rust 1.91+ to build the core executables, Node.js 22.23.4+ for docs. PDF conversion also needs Poppler and Tesseract.

1. Build the executable in a `corpora-core` checkout:

   ```sh
   cargo build --locked -p corpora-core --features spellcheck --bin corpora-text-check
   ```

2. Install and run this service:

   ```sh
   make doctor
   make setup
   export CORPORA_TEXT_CHECK_BIN=/absolute/path/to/corpora-core/target/debug/corpora-text-check
   uv run uvicorn corpora_api.app:app --reload
   ```

3. Open `http://localhost:8000/docs` or call an endpoint:

   ```sh
   curl -s http://localhost:8000/v1/checks/structure \
     -H 'Content-Type: application/json' \
     -d '{"source_id":"book.xml","text":"<root/>"}'
   ```

Set `CORPORA_DICTIONARIES_DIR` to a directory installed by [the core dictionary installer](https://github.com/exegia/corpora-core/tree/main/crates/corpora-core/tools) to enable spelling. Without it, the spelling endpoint returns 503; the other checks remain available. For a public API host, set `CORPORA_API_TOKEN`; check, conversion, and MCP requests then require `Authorization: Bearer <service token>`. `GET /v1/auth/me` instead requires a verified Supabase user access token and returns its user ID. Configure `PROJECT_REF` to select the project's JWKS and issuer, or set `SUPABASE_JWKS_URL` and `SUPABASE_JWT_ISSUER` for a self-hosted project. `SUPABASE_JWT_AUDIENCE` defaults to `authenticated`. Future user-owned routes must use `require_user` from `corpora_api.auth` and be added to the user-auth route policy. A service token never supplies a user identity; missing JWKS configuration rejects user tokens. Health, capabilities, OpenAPI, and the Swagger UI stay public.

The service limits text to one million characters. Each core request has a 10-second deadline (30 seconds for spelling). It rejects CLI errors, invalid JSON, unsupported diagnostic schema versions, and stale source hashes.

Conversion endpoints accept one multipart upload. The TEI route exposes choice, apparatus, witness, notes, and namespace policy as query parameters; EPUB offers audited `repair`; PDF offers installed OCR languages. Uploads are capped at 3 MB, TEI/EPUB at 200,000 slots, PDF at 10 pages, and returned ZIPs at 4 MB. The ZIP contains the converter's original report and bundle, including the uploaded source. PDF results with `X-Conversion-Status: gated` contain a report but no TF. These synchronous limits fit [Vercel's function payload limit](https://vercel.com/docs/functions/limitations); larger conversions need a storage-backed job API.

For local archive validation, install `packages/corpus-validate` from a matching `corpora-core` checkout into this API environment and set `CORPORA_VALIDATE_BIN` to its `corpora-validate` executable. The Vercel image installs it from the same pinned core commit as the Rust binaries.

## Docs and OpenAPI

The Astro/Fumapress site lives in `docs/`. It generates `/api` operation pages from the checked-in `docs/openapi.json` file. The pages include request bodies, response schemas, errors, examples, and the OpenAPI playground. `/mcp` on the docs host is a separate documentation-search MCP server, not the core-checking API MCP server.

The docs install applies a version-checked patch to `fumadocs-openapi` 12.6.2 so proxy-backed request examples work in its playground. Review or remove this patch when upgrading the package.

After changing Python routes or models:

```sh
uv run python scripts/export_openapi.py
make ci
```

`make ci` installs both Python and docs dependencies, runs template checks and Python tests, type-checks docs, and builds the Vercel output. The optional real-core integration test runs when `CORPORA_TEXT_CHECK_BIN` points to a built executable. The docs site can be built directly with `npm ci --prefix docs && npm run build --prefix docs`.

Fumapress AI chat is included with a request-size cap, an eight-message cap, and a five-request-per-hour Upstash limit per client. Vercel deployments use their OIDC token for AI Gateway; local runs can set `AI_GATEWAY_API_KEY`. Chat returns 503 until the docs project has `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`. Fumapress also publishes `/llms.txt` and `/llms-full.txt`.

## Deploying

The root `Dockerfile.vercel` builds a pinned commit of `corpora-core`, its three Rust executables, the converter packages, OCR system tools, and pinned dictionaries. Vercel selects its container runtime from this file. Set `CORPORA_API_TOKEN` on the API project before public deployment. The docs site is a separate Vercel project with root directory `docs` and Astro preset. Its domain is `docs.exegia.co`, with the generated API reference at `/api`.

For the frozen endpoint inventory and the routing verification and rollback process, see the [core migration cutover guide](docs/CORE-MIGRATION-CUTOVER.md).

The checked-in OpenAPI spec targets `https://api.exegia.co`; every operation path begins with `/v1/`. Vercel routes only that prefix from the existing `corpora-py` project to the new container service, preserving the older API's other endpoints. The docs and API deployments can be released independently, but the OpenAPI export must stay in sync with the Python app.

## Repository conventions

This repository was created from `exegia/corpora-template`. Work on `<type>/<slug>` branches and use Conventional Commit PR titles. Multiple feature branches are permitted only for related work in a dependent PR stack; otherwise merge the current feature into `dev` before starting another. The template release workflow and its branch policy remain in [AGENTS.md](AGENTS.md) and [`.github/WORKFLOW.md`](.github/WORKFLOW.md). Keep credentials out of Git and use `.env.example` only for placeholder configuration.

The release generator writes `CHANGELOG.md` with no trailing blank line so the release PR passes the pre-commit hygiene check. Run `make selftest` after changing that generator.

Promotion skips branches with matching trees. The release job builds Python distributions and attaches them when it creates the GitHub Release; later tag-triggered jobs handle registry publishing.
