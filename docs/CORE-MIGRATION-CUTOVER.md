# Core migration: route inventory and cutover

This is a point-in-time inventory and a preparation guide. It is not evidence of
behavioral parity, approval of a deviation, or permission to switch production routes.

## Inventory snapshot

- Observed: 2026-11-15 07:42 UTC.
- Frozen contract: [`contracts/legacy-api/operations.json`](https://github.com/exegia/corpora-core/blob/16a7cc8bf08f4b51ff618d90fd17eac3d23a5828/contracts/legacy-api/operations.json), core commit `16a7cc8bf08f4b51ff618d90fd17eac3d23a5828`.
- Compared against `https://api.exegia.co/v1/openapi.json`; response SHA-256: `2425bed419de554da304be0c18e62af74d09a7c2fcf25c2a186f8d646b956607`.
- The contract contains 63 migration operations and 14 excluded `/convert` operations.
- A present entry means only that the HTTP method and target path appear in that schema. Authentication, request and response compatibility, state, and deployed behavior remain unverified by this comparison.
- Missing means the target method and path were not in the observed schema. This inventory is not a live status feed; rerun the comparison before making decisions.

## Public health and capabilities observations

On 2026-11-15 at approximately 08:58 UTC, unauthenticated HTTPS GET requests to
`api.exegia.co` returned:

| Legacy | `/v1` target | Observation |
| --- | --- | --- |
| `/health` | `/v1/health` | Both returned HTTP 200 and `{"status":"ok"}`. |
| `/capabilities` | `/v1/capabilities` | Both returned HTTP 200 with `hub_writable: false`; legacy returned `auth_required: false`, while `/v1` returned `auth_required: true`. |

These values describe two deployments at that moment, not a general parity
result. The inspected legacy implementation derives `auth_required` from its
`AUTH_REQUIRED` setting, while the current API derives it from the presence of
`CORPORA_API_TOKEN`. Those settings govern different authentication boundaries:
the latter describes the service token, not whether user-scoped endpoints require
a Supabase identity. The differing value must be considered before routing the
legacy capabilities endpoint or changing clients. This observation does not
verify authentication failures, protected endpoints, deployment configuration,
or behavior after a route change.

| Ticket | Method | Legacy path | Target path | Schema observation |
| --- | --- | --- | --- | --- |
| 03 | `GET` | `/capabilities` | `/v1/capabilities` | Present; behavior unverified |
| 03 | `GET` | `/health` | `/v1/health` | Present; behavior unverified |
| 04 | `POST` | `/validate` | `/v1/validate` | Present; behavior unverified |
| 05 | `POST` | `/ingest` | `/v1/ingest` | Present; behavior unverified |
| 05 | `GET` | `/ingest/{job_id}` | `/v1/ingest/{job_id}` | Present; behavior unverified |
| 05 | `GET` | `/ingest/{job_id}/download` | `/v1/ingest/{job_id}/download` | Present; behavior unverified |
| 06 | `GET` | `/storage` | `/v1/storage` | Present; behavior unverified |
| 06 | `GET` | `/storage/{filename}` | `/v1/storage/{filename}` | Present; behavior unverified |
| 06 | `GET` | `/storage/{filename}/download` | `/v1/storage/{filename}/download` | Present; behavior unverified |
| 07 | `POST` | `/storage` | `/v1/storage` | Present; behavior unverified |
| 07 | `DELETE` | `/storage/{filename}` | `/v1/storage/{filename}` | Present; behavior unverified |
| 08 | `GET` | `/storage/{filename}/content` | `/v1/storage/{filename}/content` | Present; behavior unverified |
| 08 | `GET` | `/storage/{filename}/index` | `/v1/storage/{filename}/index` | Present; behavior unverified |
| 08 | `GET` | `/storage/{filename}/manifest` | `/v1/storage/{filename}/manifest` | Present; behavior unverified |
| 08 | `GET` | `/storage/{filename}/nodes/{node}` | `/v1/storage/{filename}/nodes/{node}` | Present; behavior unverified |
| 08 | `GET` | `/storage/{filename}/sections` | `/v1/storage/{filename}/sections` | Present; behavior unverified |
| 09 | `PATCH` | `/storage/{filename}/manifest` | `/v1/storage/{filename}/manifest` | Missing |
| 09 | `PATCH` | `/storage/{filename}/nodes/{node}` | `/v1/storage/{filename}/nodes/{node}` | Missing |
| 09 | `POST` | `/storage/{filename}/restore` | `/v1/storage/{filename}/restore` | Missing |
| 09 | `GET` | `/storage/{filename}/versions` | `/v1/storage/{filename}/versions` | Present; behavior unverified |
| 10 | `POST` | `/refs` | `/v1/refs` | Corpus UUID and stored revision; hosted verification pending |
| 10 | `GET` | `/refs/resolve` | `/v1/refs/resolve` | Corpus UUID and stored revision; hosted verification pending |
| 10 | `GET` | `/refs/shortcode` | `/v1/refs/shortcode` | Corpus UUID and stored revision; hosted verification pending |
| 10 | `POST` | `/refs/shortcode` | `/v1/refs/shortcode` | Corpus UUID and stored revision; hosted verification pending |
| 11 | `POST` | `/linking/{space_id}/references` | `/v1/linking/{space_id}/references` | Missing |
| 11 | `GET` | `/linking/{space_id}/references/{reference_id}` | `/v1/linking/{space_id}/references/{reference_id}` | Missing |
| 11 | `POST` | `/linking/{space_id}/references/{reference_id}/{action}` | `/v1/linking/{space_id}/references/{reference_id}/{action}` | Missing |
| 12 | `POST` | `/linking/{space_id}/browser-selection` | `/v1/linking/{space_id}/browser-selection` | Missing |
| 12 | `POST` | `/linking/{space_id}/export` | `/v1/linking/{space_id}/export` | Missing |
| 12 | `POST` | `/linking/{space_id}/publication/{reference_id}/{action}` | `/v1/linking/{space_id}/publication/{reference_id}/{action}` | Missing |
| 12 | `POST` | `/linking/{space_id}/retrieve` | `/v1/linking/{space_id}/retrieve` | Missing |
| 13 | `POST` | `/linking/{space_id}/discoveries` | `/v1/linking/{space_id}/discoveries` | Missing |
| 13 | `GET` | `/linking/{space_id}/discoveries/{discovery_id}` | `/v1/linking/{space_id}/discoveries/{discovery_id}` | Missing |
| 13 | `POST` | `/linking/{space_id}/discoveries/{discovery_id}/{action}` | `/v1/linking/{space_id}/discoveries/{discovery_id}/{action}` | Missing |
| 14 | `POST` | `/linking/{space_id}/conversion-events` | `/v1/linking/{space_id}/conversion-events` | Missing |
| 14 | `POST` | `/linking/{space_id}/export-cusx` | `/v1/linking/{space_id}/export-cusx` | Missing |
| 14 | `POST` | `/linking/{space_id}/import` | `/v1/linking/{space_id}/import` | Missing |
| 14 | `POST` | `/linking/{space_id}/import-plan` | `/v1/linking/{space_id}/import-plan` | Missing |
| 14 | `POST` | `/linking/{space_id}/scholarly-conversion-events` | `/v1/linking/{space_id}/scholarly-conversion-events` | Missing |
| 15 | `GET` | `/ai/providers` | `/v1/ai/providers` | Present; behavior unverified |
| 15 | `POST` | `/ai/validate` | `/v1/ai/validate` | Present; behavior unverified |
| 16 | `GET` | `/ai/drafts` | `/v1/ai/drafts` | Missing |
| 16 | `POST` | `/ai/drafts` | `/v1/ai/drafts` | Missing |
| 16 | `GET` | `/ai/drafts/{draft_id}` | `/v1/ai/drafts/{draft_id}` | Missing |
| 16 | `GET` | `/ai/drafts/{draft_id}/archive` | `/v1/ai/drafts/{draft_id}/archive` | Missing |
| 17 | `GET` | `/ai/drafts/{draft_id}/content` | `/v1/ai/drafts/{draft_id}/content` | Missing |
| 17 | `GET` | `/ai/drafts/{draft_id}/index` | `/v1/ai/drafts/{draft_id}/index` | Missing |
| 17 | `GET` | `/ai/drafts/{draft_id}/manifest` | `/v1/ai/drafts/{draft_id}/manifest` | Missing |
| 17 | `GET` | `/ai/drafts/{draft_id}/nodes/{node}` | `/v1/ai/drafts/{draft_id}/nodes/{node}` | Missing |
| 17 | `GET` | `/ai/drafts/{draft_id}/sections` | `/v1/ai/drafts/{draft_id}/sections` | Missing |
| 17 | `GET` | `/ai/drafts/{draft_id}/versions` | `/v1/ai/drafts/{draft_id}/versions` | Missing |
| 18 | `POST` | `/ai/chat` | `/v1/ai/chat` | Present; behavior unverified |
| 18 | `GET` | `/ai/threads` | `/v1/ai/threads` | Missing |
| 18 | `POST` | `/ai/threads` | `/v1/ai/threads` | Missing |
| 18 | `GET` | `/ai/threads/{thread_id}` | `/v1/ai/threads/{thread_id}` | Missing |
| 18 | `GET` | `/ai/threads/{thread_id}/messages` | `/v1/ai/threads/{thread_id}/messages` | Missing |
| 18 | `POST` | `/ai/threads/{thread_id}/messages` | `/v1/ai/threads/{thread_id}/messages` | Missing |
| 18 | `POST` | `/ai/threads/{thread_id}/sections` | `/v1/ai/threads/{thread_id}/sections` | Missing |
| 18 | `GET` | `/ai/threads/{thread_id}/suggestions` | `/v1/ai/threads/{thread_id}/suggestions` | Missing |
| 19 | `GET` | `/ai/changes` | `/v1/ai/changes` | Missing |
| 19 | `POST` | `/ai/changes/{change_id}/undo` | `/v1/ai/changes/{change_id}/undo` | Missing |
| 19 | `POST` | `/ai/suggestions/{suggestion_id}/apply` | `/v1/ai/suggestions/{suggestion_id}/apply` | Missing |
| 19 | `POST` | `/ai/suggestions/{suggestion_id}/reject` | `/v1/ai/suggestions/{suggestion_id}/reject` | Missing |

To reproduce the method/path comparison using an existing core checkout:

```sh
git -C /path/to/corpora-core show 16a7cc8bf08f4b51ff618d90fd17eac3d23a5828:contracts/legacy-api/operations.json > /tmp/corpora-operations.json
curl --fail --silent --show-error https://api.exegia.co/v1/openapi.json > /tmp/corpora-live-openapi.json
python3 - <<'PYCODE'
import hashlib, json
from pathlib import Path
contract = json.loads(Path("/tmp/corpora-operations.json").read_text())
raw = Path("/tmp/corpora-live-openapi.json").read_bytes()
schema = json.loads(raw)
print("schema sha256:", hashlib.sha256(raw).hexdigest())
for operation in contract["operations"]:
    method, target = operation["method"], operation["target_path"]
    present = method.lower() in schema["paths"].get(target, {})
    print(operation["ticket"], method, target, "present" if present else "missing")
PYCODE
```

## Current routing evidence

On 2026-11-15 the `api.exegia.co` domain was attached to the `corpora-py`
production deployment. Its Vercel **project routing rules**, separate from
the checked-in `vercel.json`, had live version `b200a92a-75d9-481a-87da-43193518d866`:

1. `Corpora MCP v1`: `^/v1/mcp/?$` → `https://corpora-api-tau.vercel.app/v1/mcp/`.
2. `Corpora Core API v1`: `/v1/:path*` → `https://corpora-api-tau.vercel.app/v1/:path`.

These are observations, not a permanent rollback target. Refresh the live rule
version and order before every change. An application deployment rollback alone
does not establish that separately managed routing rules have been restored.
The current repository policy assigns only `/v1/*` to this API; the legacy
service retains other paths. Ticket 20 calls for routing migrated legacy
prefixes, so resolve that policy boundary before changing any unversioned path.
`/convert` and its 14 contract operations stay on `corpora-py`.

## Per-prefix preparation and rollback

1. Identify the routing owner and capture the current production rule version,
   ordered rules, deployment IDs, destination, and a tested restoration method.
   Record any separately managed DNS, aliases, or edge rules. Do not rely on
   this snapshot as the restoration source.
2. For every operation in the proposed prefix, record its parity test or an
   explicitly approved deviation. Verify identity and owner isolation, request
   and response contracts, errors, persisted state and reload behavior, and
   representative unauthorized and revoked-access cases. Resolve storage and
   entitlement ownership before testing stateful routes. Before cutover, test a
   reconciliation or reverse migration for writes accepted after the switch,
   or prove both services use a compatible shared backing store.
3. In a non-production environment, exercise the exact proposed narrow rule
   and restoration. For any approved unversioned route, map the legacy path
   to its contract target by adding `/v1` (for example, `/refs/resolve?x=1`
   becomes `/v1/refs/resolve?x=1`). Preserve the suffix, query, authorization
   headers, body and method; check rule precedence and keep `/convert` and unrelated
   paths on the legacy service. Do not move a whole prefix while one of its
   operations is absent or lacks parity evidence.
4. After the routing owner reviews the concrete change and rollback, deploy
   one prefix at a time. Verify live success and error paths, state changes and
   reads, identity isolation, and the retained legacy `/convert` routes.
   Publish consumer guidance for the verified `/v1` alternatives.
5. If verification fails, restore the captured prior routing version and any
   separately changed deployment or alias. Verify the restored rule order,
   old endpoint behavior and `/convert`; reconcile post-cutover writes using
   the tested state plan before claiming rollback is complete. Record
   the results before attempting another switch.
