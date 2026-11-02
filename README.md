# Driver API

Standalone Django service for a deliberately small part of the driver domain. It owns driver profiles, availability, latest-location data, driver support ticket submissions and messages, and the authenticated operations listed below. `POST /api/v1/drivers/auth/register/` creates the DRIVER identity through `identity-service`, then stores vehicle and encrypted license onboarding data locally. Shared `/api/v1/auth/register/`, `/api/v1/auth/login/`, `/api/v1/auth/logout/`, token-refresh, and `/api/v1/profile/` requests are routed to `identity-service`. Driver KYC upload/status/review and support staff operations are service-owned when their admin and identity feature flags are enabled. Food-order offer and lifecycle paths are proxied to orders-service, where food and parcel assignment exclusion is enforced. Unified food-order and parcel payable history and summary are relayed to finance-service; the legacy parcel-earnings routes remain parcel-only for compatibility. Cash settlement and bonuses remain outside the ledger. Location history is durable and has a replayable historical backfill; historical support migration and general notification delivery remain open.

## Locally owned data and endpoints

`DriverProfile` stores a scalar `owner_user_id`, vehicle type and registration, an encrypted license number, verification status, availability, the legacy integer `service_radius_km`, a nullable scalar `current_order_id`, and timestamps. The service radius is exposed read-only to orders-service for dispatch eligibility. User and order IDs are not Django relations: this service neither imports nor connects to the monolith's user/order models. License numbers are encrypted with `DRIVER_FIELDS_ENCRYPTION_KEY` before database writes and are not returned by profile-read endpoints.

`DriverLocation` stores one latest coordinate pair per scalar `owner_user_id`; `DriverLocationHistory` durably stores each accepted update with its scalar active order ID and timestamp. The authenticated `POST /api/v1/drivers/location/update/` accepts `latitude` and `longitude` as finite numbers in the inclusive ranges `[-90, 90]` and `[-180, 180]`. The exact coordinates are not included in its response. Drivers can read only their own retained records from `GET /api/v1/drivers/location/history/` (`page_size` is capped at 100). In production, each update resolves the driver's active food-order or parcel assignment from orders-service using the driver's access token; the shared `DriverAssignment` table is authoritative, rather than the legacy profile's `current_order_id`. A valid update publishes `driver_location` to the shared `driver_{driver_id}` Channels group for admin/vendor live-map consumers and, when assigned, the legacy `driver_location_update` event to `order_{order_id}` for customer tracking. The event coordinate values are latitude/longitude in that order. Assignment lookup failures return HTTP 503 before the location is persisted; configure `ORDERS_SERVICE_URL` and keep its active-assignment route available. Only isolated DEBUG runs without an orders-service URL use the local profile field for compatibility. On successful publication, a normal update response retains `{"success": true, "buffered": true}`; invalid coordinates return HTTP 400, and authentication is required. Missing/failing event publication returns HTTP 503 so clients can retry; the accepted location remains durably recorded even when the realtime broker is unavailable. The service persists legacy `MIN_INTERVAL`, `GEOBOUNDARY_EXIT`, `SPEED_EXCEEDED`, `IMPOSSIBLE_TRAVEL`, `STATIONARY_SPOOFING`, and `STATIC_LOCATION` anomaly records in `DriverLocationAnomaly`; critical anomalies retain the legacy HTTP 422 rejection response, while non-critical anomalies are returned as `anomalies_detected` and `anomaly_types`. This replaces the old Redis list plus scheduled monolith flush. Use `backfill_driver_location_history` to dry-run and reconcile historical order-linked logs, then pass `--apply` only after all unattributed logs are resolved. `purge_driver_location_history` is dry-run by default and uses the legacy 90-day retention policy; pass `--apply` to delete expired rows and monitor a daily owner-service schedule before production cutover. `backfill_driver_location_anomalies` provides a dry-run and insert-only copy of legacy anomaly rows; the data-backed reconciliation and legacy fraud-alert/case workflow remain open.

For production, configure `CHANNEL_REDIS_URL` (or `REDIS_URL`) to the same Redis channel-layer backend used by the WebSocket service, and `FINANCE_SERVICE_URL` to the finance-service origin for driver earnings reads. The channel names and messages are part of the integration contract:

```json
{"group":"driver_{driver_id}","type":"driver_location","latitude":-17.825,"longitude":31.033}
{"group":"order_{order_id}","type":"driver_location_update","lat":-17.825,"lng":31.033}
```

Driver availability for pricing demand calculations is exposed only through the internal `GET /internal/v1/drivers/demand/` endpoint. It counts verified, available profiles whose identity-service account is active. The count endpoint and its identity-service lookup require `X-Demand-Sync-Token`; configure the same high-entropy token on pricing-service, orders-service, driver-api, and identity-service. Identity lookup failures return HTTP 503 rather than a potentially incorrect count. No driver identity or location data is exposed by the count endpoint.

Orders-service owns the active assignment mapping, and driver-api resolves it before linking a persisted location update or publishing an order-group event. Historical order-linked location records still require ID-preserving reconciliation before cutover.

All local profile and operations endpoints require an access token accepted by `bikadeli_common.authentication.AccessTokenAuthentication`; driver registration is public and delegates identity creation to `identity-service`:

| Endpoint | Behavior |
| --- | --- |
| `POST /api/v1/drivers/profile/` | Create the caller's profile (vehicle type, optional registration, and optional write-only license number); starts as `PENDING` and unavailable. Matching retries return the existing profile. |
| `POST /api/v1/drivers/auth/register/` | Public driver registration; creates the DRIVER identity in `identity-service` and stores a pending local profile. Passwords must be at least 12 characters, matching identity-service policy. |
| `POST /api/v1/drivers/profile/submit_verification/` | Authenticated multipart upload of one or more KYC images; stores encrypted private files and resets review status to `PENDING`. Requires `DRIVER_KYC_SERVICE_ENABLED=true`. |
| `GET /api/v1/drivers/profile/kyc/` | Return the caller's KYC document statuses and completeness without exposing file paths or contents. Requires `DRIVER_KYC_SERVICE_ENABLED=true`. |
| `GET /api/v1/drivers/profile/` or `/api/v1/drivers/profile/me/` | Read only the caller's profile. |
| `PATCH /api/v1/drivers/profile/` | Update only `vehicle_registration`. |
| `PUT /api/v1/drivers/profile/update_profile/` | Compatibility route for that same limited profile update. |
| `POST /api/v1/drivers/profile/toggle_online/` | Set `is_online` to a JSON boolean; only `VERIFIED` profiles can become available. |
| `POST /api/v1/drivers/location/update/` | Persist the caller's latest coordinates and publish live-map/customer tracking events without returning coordinates. |
| `GET /api/v1/drivers/location/history/` | Paginated history of the authenticated driver's own location updates; coordinates are sensitive and are never exposed cross-driver. |
| `GET /internal/v1/admin/drivers/location-anomalies/` | Filter and page service-owned location anomalies; requires the fraud-review feature flag and internal admin token. |
| `POST /internal/v1/admin/drivers/location-anomalies/{anomaly_id}/review/` | Record a single actor-attributed review; repeated review attempts return HTTP 409. |
| `GET, POST /api/v1/drivers/support-tickets/` | List the caller's tickets (optionally filtered by status) or create a ticket with an initial driver message. Optional `order_id` values are accepted only after orders-service confirms an assignment to the authenticated driver. |
| `GET /api/v1/drivers/support-tickets/{ticket_id}/messages/` | Read a ticket thread only when the caller owns the ticket. |
| `POST /api/v1/drivers/support-tickets/{ticket_id}/reply/` | Add a driver message to the caller's ticket; a resolved/closed ticket is reopened as `IN_PROGRESS`. |
| `GET /internal/v1/admin/drivers/support-tickets/` | Internal admin-api list; requires `X-Driver-Admin-Token`. Supports status/priority/category/search/assignment filters and bounded pagination. |
| `POST /internal/v1/admin/drivers/support-tickets/create/` | Create and audit a staff-created ticket; requires `X-Driver-Admin-Token`, actor headers, and identity projection lookup configuration. |
| `POST /internal/v1/admin/drivers/support-tickets/bulk/` | Atomically assign selected driver tickets or record an export audit; requires the admin token and actor headers. |
| `POST /internal/v1/admin/drivers/support-tickets/{ticket_id}/attachments/` | Upload up to five JPEG, PNG, WebP, or PDF attachments (5 MB each); validates file signatures and records the actor audit. |
| `GET /internal/v1/admin/drivers/support-tickets/{ticket_id}/attachments/{attachment_id}/content/` | Return an audited attachment as a private no-store download; requires the admin token and actor headers. |
| `GET /internal/v1/admin/drivers/support-tickets/{ticket_id}/` | Internal ticket details, messages, internal notes, and audited status history; requires the admin token. |
| `POST /internal/v1/admin/drivers/support-tickets/{ticket_id}/reply/` | Internal staff reply; requires admin token plus actor ID/email headers and persists a support message and audit event. |
| `PATCH /internal/v1/admin/drivers/support-tickets/{ticket_id}/` | Internal status/resolution-note update with actor audit. |
| `POST /internal/v1/admin/drivers/support-tickets/{ticket_id}/{assign,escalate,internal-notes}/` | Internal assignment, escalation, and private-note actions with actor audit. |
| `GET /health/`, `GET /health/ready/` | Liveness and database readiness. |

For orders-service integration, the authenticated `GET /api/v1/drivers/profile/me/` route is kept at the path currently called by `orders-service/parcels/integrations.py`. It returns a JSON object with integer `id` (required by that caller), `owner_user_id`, `vehicle_type`, `vehicle_registration`, `verification_status`, `is_available`, nullable scalar `current_order_id`, `created_at`, and `updated_at`. A missing profile returns `404`. There is no separate eligibility URL: the profile response carries the verification/availability fields for callers to evaluate, while the availability mutation itself enforces `verification_status == "VERIFIED"`. Names are not copied from the identity service, so `first_name`/`last_name` are intentionally absent; the current orders-service caller treats them as optional. The local profile primary key must be backfilled/mapped before orders-service records can safely use it as `driver_profile_id`.

## Compatibility gaps and rollout limits

Other `/api/v1/drivers/*` requests still use the compatibility proxy to `MONOLITH_BASE_URL`; they are not owned by this service. In particular, login compatibility aliases, profile fields outside this service's small subset, anomaly history reconciliation and alert integration, bonuses, quests, and promos remain open. Driver support tickets and location-anomaly records are local writes. Admin-api routes the explicit `ticket_type=driver` inbox filter, driver ticket creation, attachments, bulk assignment/export audit, detail/actions to their respective internal endpoints when their feature flags and `ADMIN_IDENTITY_AUTH_ENABLED=true`; reads require the appropriate support or driver-verification grant, while mutations require the appropriate write grant, fresh MFA, and throttling. `DRIVER_FRAUD_REVIEW_SERVICE_ENABLED` is separate and defaults off. The admin UI exposes the anomaly queue only through this gated service route. Attachment downloads also require fresh MFA and generate an audit event. Ticket creation resolves an active `DRIVER` identity through identity-service's token-protected driver lookup and then requires a local driver profile. Attachments are encrypted in private storage and are not exposed through public file URLs. `DRIVER_ADMIN_TOKEN` and `IDENTITY_USER_LOOKUP_TOKEN` must be high-entropy private secrets; identity-service and driver-api must receive the same identity lookup token. The current service does not send reply notifications or migrate historical tickets; these are cutover blockers. Food-order API ownership resides in orders-service and its shared assignment table. `/api/v1/drivers/earnings/` is explicitly routed to finance-service and never falls through to the monolith; the legacy `/api/v1/drivers/parcel-earnings/` path is also explicitly routed and retains its parcel-only response. Configure `FINANCE_SERVICE_URL` in production. If identity creation succeeds but the local profile database is unavailable, the registration response is an explicit 503; the driver can log in and retry `POST /api/v1/drivers/profile/`, which is idempotent for matching vehicle/license details and returns 409 when a retry changes them. The location update and history routes are explicitly resolved locally before the catch-all driver proxy. Parcel-order routes are sent to `ORDERS_SERVICE_URL`; a missing URL returns an explicit 503 rather than falling back to the backend.

No historical data has been backfilled and no production routing cutover has been performed. Newly created profiles start pending. Admin KYC queue/detail/review routes require `DRIVER_KYC_SERVICE_ENABLED=true`, `ADMIN_IDENTITY_AUTH_ENABLED=true`, identity authorization for `drivers:verify`/`drivers:write`, fresh MFA for document-content access and review mutations, and matching private `DRIVER_ADMIN_TOKEN` values between admin-api and driver-api. KYC uploads accept JPEG, PNG, or WebP images up to 10 MB and 20 megapixels; content is encrypted at rest with `DRIVER_FIELDS_ENCRYPTION_KEY` and stored without a public URL. Key rotation requires re-encrypting retained documents before replacing the key. When configured, new uploads are scanned through ClamAV's INSTREAM protocol; a missing or unavailable scanner fails closed outside DEBUG, while isolated DEBUG may bypass scanning. Configure `DRIVER_KYC_SCANNER_HOST` before enabling KYC outside DEBUG. The scoped backfill covers legacy profile-photo fields and versioned `DriverDocument` records, but source S3/object storage, reviewer email and separate admin audit-event history, scanner deployment, and data-backed reconciliation remain cutover blockers. Existing approved drivers need an approved profile and KYC migration/reconciliation before they can toggle availability here. Do not treat this API as a full replacement for the monolith driver API.

### Driver profile ID-preserving backfill

After creating the destination schema with this service's migrations, `backfill_driver_profiles` can copy rows from the monolith's `drivers_driverprofile` table. It preserves profile primary keys, stores the source identity ID as `source_user_id`, and sets `owner_user_id` only from a reviewed source-to-destination identity CSV. It calls identity-service's token-protected active-driver lookup to reject destination IDs that are not active drivers. The CSV establishes the source/destination pairing; it must be reconciled against authoritative identity records before use. The legacy current-order ID is retained only as `source_current_order_id`; `current_order_id` stays unset because orders-service owns active assignments and no cross-domain order mapping is inferred. The command copies the fields this service owns, including license numbers (encrypted on destination write), and supports PostgreSQL or SQLite source/destination databases. It defaults to a read-only dry run; `--apply` inserts missing profiles only, inside a destination transaction. Existing rows are never updated: a profile-ID mismatch or an owner-user-ID collision aborts the run. Reports include source/already-matching/insertable counts and keyed HMAC-SHA-256 fingerprints so low-entropy license numbers are not exposed by plain hashes.

Set `SOURCE_DATABASE_URL` to the source and `DATABASE_URL` to the destination explicitly. The command refuses URLs identifying the same database. First run and review the dry-run output:

```powershell
$env:SOURCE_DATABASE_URL = "postgresql://<user>:<password>@<source-host>:5432/<source-database>"
$env:DATABASE_URL = "postgresql://<user>:<password>@<destination-host>:5432/<destination-database>"
$env:IDENTITY_MAPPING_CSV = "<reviewed-source_user_id,destination_user_id-csv>"
$env:IDENTITY_SERVICE_URL = "https://<identity-service>"
$env:DEMAND_SYNC_TOKEN = "<identity-service-internal-token>"
python manage.py backfill_driver_profiles
```

Only run the insert step as an explicitly approved migration operation after reviewing the report and provisioning/migrating the destination:

```powershell
python manage.py backfill_driver_profiles --apply
```

The identity CSV must contain exactly the `source_user_id,destination_user_id` headers, map every source driver once, and have unique destination IDs. The command verifies mapped destination IDs through identity-service in batches of at most 1000; it does not prove that the CSV pairing itself is correct. The local tests cover distinct source/destination IDs, inactive target rejection, idempotent reruns, and preservation of the untrusted legacy order reference as provenance. No real migration data or production identity endpoint has been read.

### Legacy driver KYC backfill

After the destination driver's profiles have been copied with `backfill_driver_profiles` and this service's migrations have been applied, `backfill_driver_profile_kyc` copies both image files referenced by the legacy `drivers_driverprofile` fields (`license_photo`, `id_document`, `vehicle_photo`, `registration_book`, `selfie`, and `selfie_holding_id`) and the legacy `drivers_driverdocument` version history. It preserves profile IDs and stores source document IDs as scalar mappings, along with document types, versions/current flags, file hashes, expiry and review status/reason/timestamps. It requires the destination profile's `source_user_id` to match the legacy profile owner and uses the mapped destination owner ID. Legacy reviewer IDs are stored separately as source IDs; they are not treated as verified identity-service IDs until reviewer mapping is separately reconciled. The historical document types are namespaced as `LEGACY_*` so they do not collide with the six current driver-upload document types. Destination files are encrypted in private storage. Legacy profile-photo review statuses are preserved for vehicle photos and selfie-with-ID only; other profile-photo records are initialized as `PENDING`.

Set `SOURCE_DATABASE_URL` and `DATABASE_URL` explicitly; they must identify different databases. The command accepts PostgreSQL or SQLite databases. Choose one source media backend:

- `SOURCE_MEDIA_BACKEND=filesystem` (the default): set `SOURCE_MEDIA_ROOT`; optionally set `SOURCE_ID_MEDIA_ROOT` for a separate ID-document directory. Both roots must exist and must not overlap the destination `KYC_MEDIA_ROOT`.
- `SOURCE_MEDIA_BACKEND=s3`: set `SOURCE_S3_BUCKET`; optionally set `SOURCE_ID_S3_BUCKET` for a separate ID-document bucket (otherwise the default bucket is used), `SOURCE_S3_REGION`, and `SOURCE_S3_ENDPOINT_URL` for an S3-compatible endpoint. Authentication uses the AWS SDK credential chain. Grant the migration identity only `s3:GetObject` on the required object prefixes; the command does not list or write objects. Never put access keys in the command line or migration report.

Legacy profile photos must be valid JPEG, PNG, or WebP images up to 100 MB and 20 megapixels. Versioned `DriverDocument` files up to 100 MB are copied as private octet-stream content unless a PDF or supported image type can be identified; every non-empty file is malware-scanned. Outside DEBUG, configure `DRIVER_KYC_SCANNER_HOST`; scanner failure stops the run. The local Compose stack provides an isolated ClamAV service for development and rehearsal; production scanner deployment and health must be verified independently. The DEBUG-only scanner bypass is suitable only for isolated tests, not a migration rehearsal.

The command is read-only by default. Review the report before applying it to an isolated, approved non-production dataset:

```powershell
$env:SOURCE_DATABASE_URL = "postgresql://<user>:<password>@<source-host>:5432/<source-database>"
$env:DATABASE_URL = "postgresql://<user>:<password>@<destination-host>:5432/<destination-database>"
$env:SOURCE_MEDIA_BACKEND = "filesystem"
$env:SOURCE_MEDIA_ROOT = "<read-only-source-driver-media-root>"
$env:SOURCE_ID_MEDIA_ROOT = "<optional-read-only-source-id-media-root>"
$env:KYC_MEDIA_ROOT = "<private-destination-kyc-storage>"
python manage.py backfill_driver_profile_kyc
python manage.py backfill_driver_profile_kyc --apply
```

For S3, set `SOURCE_MEDIA_BACKEND=s3`, `SOURCE_S3_BUCKET`, and optionally `SOURCE_ID_S3_BUCKET`, `SOURCE_S3_REGION`, and `SOURCE_S3_ENDPOINT_URL`; provide credentials through the AWS SDK credential chain. The offline source-media tests use botocore stubs and make no network requests.

`--apply` inserts missing documents only, in a destination transaction; it does not overwrite or delete existing documents. An existing row with different metadata or content aborts reconciliation. Reruns verify matching rows and encrypted file contents. Reports use keyed HMAC fingerprints rather than plain content hashes. The local integration tests cover dry-run, apply, rerun reconciliation, encrypted destination files, document version/reviewer/expiry metadata, fileless document rows, and rejection of unsafe media paths. This command has not been run against production or real migration data.

This is not a complete historical KYC migration. Reviewer identity/email mapping and separate admin audit-event history are not imported. S3 support reads configured objects but has not been exercised against a real source bucket; any rehearsal must use an approved, read-only non-production identity and dataset. Those data gaps must be reconciled and rehearsed on non-production data before cutover.

### Historical driver-location anomaly backfill

After driver profiles have been copied with verified identity mappings and this service's migrations have been applied, `backfill_driver_location_anomalies` reads the monolith's `fraud_driverlocationanomaly` rows. It maps each source driver through `DriverProfile.source_user_id` to the destination owner ID and preserves both the source owner and anomaly IDs, measured locations/speed/distance/time, severity/type/details, source order/dispute IDs, review state/note, creation time, and source reviewer IDs. Source order and reviewer IDs are retained as provenance fields only: `active_order_id` and the trusted reviewer identity remain unset until their respective mappings are verified. The legacy anomaly model has no review timestamp to migrate.

Set `SOURCE_DATABASE_URL` and `DATABASE_URL` to different databases. Driver profiles for every source anomaly owner must already exist in the destination with matching `source_user_id` provenance. PostgreSQL sources use PostGIS point coordinates; SQLite sources must expose scalar `reported_latitude`/`reported_longitude` and `previous_latitude`/`previous_longitude` columns. The command is read-only by default and supports insert-only `--apply` with rerun reconciliation:

```powershell
$env:SOURCE_DATABASE_URL = "postgresql://<user>:<password>@<source-host>:5432/<source-database>"
$env:DATABASE_URL = "postgresql://<user>:<password>@<destination-host>:5432/<destination-database>"
python manage.py backfill_driver_location_anomalies
python manage.py backfill_driver_location_anomalies --apply
```

The local integration test verifies dry-run, apply, rerun, coordinate/measurement preservation, and source-order/reviewer lineage. No real legacy dataset has been read or written. Existing monolith fraud alerts/cases, alert routing, and reviewer identity mapping are not imported by this command and remain cutover blockers.

## Configuration

- `DJANGO_SECRET_KEY`: required when `DJANGO_DEBUG` is false.
- `JWT_PUBLIC_KEYS_JSON`: required in production; JSON object mapping accepted identity key IDs to PEM-encoded RSA public keys. Never configure the identity private signing key on driver-api.
- `JWT_ISSUER`: required outside local debug mode; configure the same issuer as identity-service.
- `DATABASE_URL`: required outside debug mode; supports PostgreSQL or SQLite. `DATABASE_SSLMODE` optionally overrides PostgreSQL's default `require`.
- `ALLOWED_HOSTS`: comma-separated hosts; configure actual service hostnames outside local development.
- `DJANGO_DEBUG`: set to `true` for local development only.
- `MONOLITH_BASE_URL`: compatibility upstream for the driver operations that have not yet been extracted.
- `ORDERS_SERVICE_URL`: required in production; base URL for orders-service parcel
  requests, active-assignment lookup, and driver order ownership checks.
- `DRIVER_KYC_SCANNER_HOST`: required when `DRIVER_KYC_SERVICE_ENABLED=true`
  outside DEBUG; hostname of a trusted ClamAV daemon.
- `DRIVER_KYC_SCANNER_PORT` and `DRIVER_KYC_SCANNER_TIMEOUT_SECONDS`: optional
  scanner settings, defaulting to `3310` and `5` seconds respectively.
- `IDENTITY_SERVICE_URL`: required in production; base URL for shared account authentication and basic identity profile requests.
- `JWT_VALIDATE_IDENTITY_SESSIONS`: keep enabled in production so logout and account deactivation revoke RS256 sessions across APIs; requires the HTTPS `IDENTITY_SERVICE_URL`.
- `CHANNEL_REDIS_URL` or `REDIS_URL`: required in production; shared Redis channel-layer URL for live location events.
- `DEMAND_SYNC_TOKEN`: required in production; at least 32 characters and shared with pricing-service and orders-service for the internal driver-demand count.
- `IDENTITY_USER_LOOKUP_TOKEN`: required when `DRIVER_SUPPORT_SERVICE_ENABLED=true`; at least 32 characters and must match identity-service's user lookup token.
- `DRIVER_FIELDS_ENCRYPTION_KEY`: required in production; a Fernet key used to encrypt driver license numbers. Use a distinct key managed by the deployment secret store; do not reuse the development fallback.
- `UPSTREAM_TIMEOUT_SECONDS`: optional positive proxy timeout (default `10`).
- `LOG_LEVEL`: optional log level (default `INFO`).

## Build and run

Build from the `bikadeli-services` workspace root so the Dockerfile can include `common-services`:

```powershell
docker build -f driver_api/Dockerfile -t bikadeli-driver-api:local .
```

Configure the environment above, then apply this service's migrations as a deliberate deployment step (not against production as part of this change). Set the container environment variables in the deployment environment before running:

```powershell
$env:DJANGO_SECRET_KEY = "<deployment-secret>"
$env:JWT_PUBLIC_KEYS_JSON = '{"identity-key":"<PEM-public-key>"}'
$env:JWT_ISSUER = "<identity-issuer>"
$env:DATABASE_URL = "postgresql://<user>:<password>@<host>:5432/<database>"
$env:DEMAND_SYNC_TOKEN = "<shared-high-entropy-secret>"
$env:IDENTITY_USER_LOOKUP_TOKEN = "<identity-user-lookup-secret>"
$env:DRIVER_FIELDS_ENCRYPTION_KEY = "<Fernet-key-from-secret-store>"
$env:ALLOWED_HOSTS = "<service-hostname>"
docker run --rm --env DJANGO_SECRET_KEY --env JWT_PUBLIC_KEYS_JSON --env JWT_ISSUER --env DATABASE_URL --env DRIVER_FIELDS_ENCRYPTION_KEY --env IDENTITY_USER_LOOKUP_TOKEN --env ALLOWED_HOSTS --entrypoint python bikadeli-driver-api:local manage.py migrate --noinput
docker run --rm -p 8000:8000 --env DJANGO_SECRET_KEY --env JWT_PUBLIC_KEYS_JSON --env JWT_ISSUER --env DATABASE_URL --env DRIVER_FIELDS_ENCRYPTION_KEY --env IDENTITY_USER_LOOKUP_TOKEN --env ALLOWED_HOSTS bikadeli-driver-api:local
```

Run locally from `driver_api` after installing `requirements.txt`:

```powershell
$env:DJANGO_DEBUG = "true"
$env:JWT_SIGNING_KEY = "<shared-local-only-development-key>"
python manage.py migrate
python manage.py runserver
```

The local debug database defaults to `driver-api.sqlite3` in this service directory. The image serves with Gunicorn on port `8000`. For local tests, use `DJANGO_DEBUG=true` and an in-memory SQLite database:

```powershell
Set-Location driver_api
$env:DJANGO_DEBUG = "true"
$env:DATABASE_URL = "sqlite:///:memory:"
python manage.py test tests
```
