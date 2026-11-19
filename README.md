# PVC Monitor

A web application for monitoring Kubernetes PersistentVolumes backed by the vSphere CSI driver. Provides a unified view of PVCs, their corresponding vSphere First Class Disks (FCDs), FCD-level snapshots, disks that no configured cluster claims, and an overview page to spot unused or stale volumes. It reads from vCenter and Kubernetes only; it never changes anything in either.

## Features

| Tab | What it shows |
|-----|---------------|
| **Overview** | vSphere CSI PVCs of every configured cluster joined with vCenter FCD data — usage state: Active / Idle / Released / Pending / Unknown |
| **Persistent Volumes** | vSphere FCDs matched to the PVs of every configured cluster by volume handle — datastore, capacity, VM attachment, power state |
| **Container Volumes** | Kubernetes PVCs backed by the vSphere CSI driver in every configured cluster |
| **Snapshots** | FCD-level snapshots created via the Kubernetes VolumeSnapshot API — age badges, old-snapshot email alerts |
| **Unclaimed Disks** | FCDs that no configured cluster claims, each with a category and safeguards, with email alerts; see [Unclaimed disks](#unclaimed-disks) |
| **K8s Volumes** | Raw Kubernetes PersistentVolumes and PVCs of the first configured cluster |
| **K8s Snapshots** | VolumeSnapshot and VolumeSnapshotContent objects of the first configured cluster |

Disks are matched to PVs only by UUID: a CSI PV's `spec.csi.volumeHandle` must equal the FCD id. Display names of disks, PVs or PVCs are never used for matching, so a disk or claim named like another volume cannot change which PV a disk is attributed to. An in-tree `kubernetes.io/vsphere-volume` PV counts as a claim on the FCD whose backing file path equals the PV's `volumePath` exactly (case-insensitive).

### Email alerts

Two independent alert types share the same SMTP configuration and run on the same check interval:

| Alert | Trigger | Subject line |
|-------|---------|--------------|
| **Snapshot age** | Any FCD snapshot older than `ALERT_SNAPSHOT_AGE_DAYS` (default 7 days) | `[PVC Monitor] N snapshots older than X days` |
| **Unclaimed disks** | Any disk that [needs investigation](#safeguards); other categories are listed as counts | `[PVC Monitor] N disks not claimed by any configured cluster need investigation` |

Each alert tab has a **Test Alert** button that sends a one-off email immediately, even when there is nothing to report.

### Authentication

Login through an OIDC provider (for example Dex) is on by default, with optional group-based access control (`ALLOWED_GROUPS`). The application refuses to start when login is enabled but not fully configured; running without login requires `AUTH_ENABLED=false`. When a user's groups do not intersect with the allowed set, they see a 403 page listing their groups and the required ones, right after the denied sign-in only. When the sign-in itself fails (for example a misconfigured issuer), a short error page with a retry link is shown instead of an automatic redirect to the login, which could loop; the detail is in the server log.

## Architecture

```
┌─────────────────────────────────────────┐
│  React (Vite)  ── served by FastAPI     │
├─────────────────────────────────────────┤
│  FastAPI backend                        │
│  ├── pyVmomi  → vCenter SOAP API        │
│  ├── kubernetes-client → K8s API        │
│  └── authlib  → OIDC provider (Dex)     │
└─────────────────────────────────────────┘
```

Built as a single Docker image (multi-stage): Node 20 builds the React app, Python 3.12 serves it via FastAPI/uvicorn.

## Prerequisites

- vCenter with a dedicated service account and a custom role, see [Required vCenter privileges](#required-vcenter-privileges)
- Kubernetes cluster using the vSphere CSI driver (`csi.vsphere.vmware.com`)
- The external-snapshotter CRDs installed if you want snapshot data
- An OIDC provider (for example Dex) for login, unless you run with `AUTH_ENABLED=false` for local development

### Required vCenter privileges

The service account is **not read-only**. According to the vSphere Web Services API reference, the First Class Disk calls this application makes (`ListVStorageObject`, `RetrieveVStorageObject`, `RetrieveSnapshotInfo`) require the `Datastore.FileManagement` privilege ("Low level file operations") on the datastore. That is the same privilege that deleting a First Class Disk or one of its snapshots requires, so whoever holds this account's password can delete disks and snapshots on those datastores. Treat the password accordingly.

Recommended set-up:

1. Create a dedicated account for this application. Do not use an administrator account.
2. Create a custom role that contains only **Datastore > Low level file operations** (`Datastore.FileManagement`). The system privileges every role carries (`System.Anonymous`, `System.View`, `System.Read`) are added automatically.
3. Assign that role to the account on the datastores (or the datastore folder) that the vSphere CSI driver uses, not on the vCenter root.
4. Assign the built-in **Read-only** role to the account on the VM folder that holds the Kubernetes node VMs, so the application can see which disks are attached.

A datastore where the account cannot list First Class Disks is reported as a problem with the result (and logged with a hint about `Datastore.FileManagement` whenever vCenter answers `NoPermission`), never skipped silently. If no datastore can be listed at all, the views return an error instead of an empty list. Use `VCENTER_DATASTORES` to limit the scan to the datastores the vSphere CSI driver uses, and grant the privilege only there.

**Cloud Native Storage.** The unclaimed-disk view queries Cloud Native Storage (CNS) for the cluster each disk belongs to (`VCENTER_CNS_ENABLED`, on by default). According to VMware documentation, viewing CNS volumes needs the **Cns.Searchable** privilege ("CNS > Searchable"); grant it where the VMware documentation for your vSphere version says (not verified here). pyVmomi's type definitions list the same privilege for the query call (`CnsQueryVolume`). Whether that privilege alone is enough for the query API has **not been verified** against a live vCenter. If the query fails, the list is still shown; see [Unclaimed disks](#unclaimed-disks).

## Configuration

All configuration is via environment variables. Copy `backend/.env.example` for local development.

### vCenter

| Variable | Description | Default |
|----------|-------------|---------|
| `VCENTER_HOST` | vCenter hostname (must match the certificate) | — |
| `VCENTER_USER` | Service account UPN | — |
| `VCENTER_PASSWORD` | Service account password | — |
| `VCENTER_PORT` | vCenter HTTPS port | `443` |
| `VCENTER_VERIFY_SSL` | Verify the certificate chain and hostname. `false` disables verification and logs a warning at start; the password can then be intercepted | `true` |
| `VCENTER_CA_FILE` | PEM bundle with the CA that signed the vCenter certificate (for example the VMCA root). Empty uses the system trust store | — |
| `VCENTER_CACHE_SECONDS` | How long a finished vCenter scan is reused. Only one scan runs at a time; requests that arrive during a scan get its result | `60` |

Every vCenter request has a 60-second socket timeout, and every session is logged out after use.

### Kubernetes

| Variable | Description |
|----------|-------------|
| `K8S_SERVER` | API server URL (OIDC / external clusters) |
| `K8S_TOKEN` | Bearer token |
| `K8S_CA_FILE` | CA bundle for `K8S_SERVER` (used with `K8S_SERVER`/`K8S_TOKEN` only) |
| `K8S_VERIFY_SSL` | Verify TLS for `K8S_SERVER` (default `true`; `false` logs a warning) |
| `KUBECONFIG_PATH` | Path to a kubeconfig file |
| `K8S_CONTEXT` | Context within the kubeconfig |
| `K8S_CLUSTER_NAME` | Display name of this single cluster (default `default`) |
| `K8S_CSI_CLUSTER_ID` | `cluster-id` from this cluster's vSphere CSI configuration (`[Global] cluster-id` in `csi-vsphere.conf`) |

When none of the connection variables are set the app uses in-cluster config (mounted ServiceAccount token), which is the default when deployed in Kubernetes.

List calls are paged and time out after 60 seconds. If the PersistentVolume list cannot be read completely, the request fails rather than working with a partial list, because that list decides which disks count as claimed.

#### Several clusters

When several Kubernetes clusters use the same vCenter, every one of them must be configured; otherwise the disks of the missing clusters are reported as not claimed. Describe them in `K8S_CLUSTERS`, a JSON list (it replaces the variables above, which must then be empty):

```bash
K8S_CLUSTERS='[
  {"name": "local", "in_cluster": true, "csi_cluster_id": "cluster-a"},
  {"name": "cluster-b", "kubeconfig": "/etc/pvc-monitor/kubeconfigs/cluster-b", "context": "b-admin", "csi_cluster_id": "cluster-b"}
]'
```

| Key | Required | Meaning |
|-----|----------|---------|
| `name` | yes | Display name, unique |
| `in_cluster` | one of these two | `true`: use the pod's ServiceAccount |
| `kubeconfig` | one of these two | Path to a kubeconfig file; it must exist at start |
| `context` | no | Context within that kubeconfig (default: its current context) |
| `csi_cluster_id` | no, but needed to place disks with CNS | `cluster-id` from that cluster's vSphere CSI configuration; unique |

The application refuses to start with a message naming the problem when the JSON is invalid, a key is unknown, a name or `csi_cluster_id` repeats, or a kubeconfig is missing. Kubeconfigs must embed their credentials (token or client certificate) and CA; exec and auth-provider plugins are not available in the image.

The first cluster in the list is the one shown on the **K8s Volumes** and **K8s Snapshots** tabs and in the K8s health badge. All other views use every cluster and show a Cluster column.

Each remote cluster needs a read-only identity with this ClusterRole (the application only lists):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: pvc-monitor-reader
rules:
  - apiGroups: [""]
    resources: ["persistentvolumes", "persistentvolumeclaims"]
    verbs: ["list"]
  - apiGroups: ["snapshot.storage.k8s.io"]
    resources: ["volumesnapshots", "volumesnapshotcontents"]
    verbs: ["list"]
```

Bind it to a ServiceAccount in that cluster and put a kubeconfig with a token for that ServiceAccount in the Secret named by `clusters.kubeconfigSecret` (see [Deploying to Kubernetes](#deploying-to-kubernetes)).

### Unclaimed disks

| Variable | Description | Default |
|----------|-------------|---------|
| `VCENTER_DATASTORES` | Comma-separated datastore names to scan; empty scans every datastore the account can see. A name that is not found is reported as a problem | — |
| `VCENTER_CNS_ENABLED` | Query Cloud Native Storage for the cluster each disk belongs to | `true` |
| `UNCLAIMED_MIN_AGE_HOURS` | Disks younger than this are flagged `too_young` and never need investigation | `24` |

See [Unclaimed disks](#unclaimed-disks) for what the result means.

### Email alerts

| Variable | Description | Default |
|----------|-------------|---------|
| `SMTP_HOST` | SMTP server hostname | — |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USE_TLS` | Use STARTTLS (port 465 always uses implicit TLS) | `true` |
| `SMTP_FROM` | Sender address | — |
| `SMTP_USER` | SMTP username (optional) | — |
| `SMTP_PASSWORD` | SMTP password (optional) | — |
| `ALERT_EMAIL_TO` | Comma-separated recipients | — |
| `ALERT_SNAPSHOT_AGE_DAYS` | Age threshold for snapshot alerts | `7` |
| `ALERT_CHECK_INTERVAL_HOURS` | How often to run both alert checks (minimum `1`) | `24` |

Alerts are disabled when `SMTP_HOST`, `SMTP_FROM` or `ALERT_EMAIL_TO` is empty. Both alert types (snapshot age and unclaimed disks) use the same SMTP settings and run on the same interval. Each has a **Test Alert** button in the UI that triggers an immediate test email; each kind can be triggered at most once per minute.

The SMTP server certificate and hostname are always verified. For a relay with a private CA, add the CA to the image trust store or point the standard `SSL_CERT_FILE` variable at a bundle. Credentials are never sent without TLS: with `SMTP_USE_TLS=false` on a port other than 465, leave `SMTP_USER`/`SMTP_PASSWORD` empty (unauthenticated relay only). A snapshot whose creation time cannot be read is included in the alert with age "unknown".

Invalid numeric or boolean settings stop the application at start with a message naming the variable.

### OIDC authentication

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTH_ENABLED` | Require login. `false` serves every route without login and logs a warning | `true` |
| `OIDC_ISSUER` | Issuer URL; must be `https://` (`http://` only for localhost). Required when `AUTH_ENABLED=true` | — |
| `OIDC_CLIENT_ID` | OAuth2 client ID. Required when `AUTH_ENABLED=true` | — |
| `OIDC_CLIENT_SECRET` | OAuth2 client secret. Required when `AUTH_ENABLED=true` | — |
| `OIDC_REDIRECT_URI` | Full callback URL; set it explicitly in production | derived from the request |
| `OIDC_SCOPES` | Requested scopes | `openid email profile groups` |
| `ALLOWED_GROUPS` | Comma-separated groups allowed to log in | _(all users)_ |
| `SESSION_SECRET` | Cookie signing key, at least 32 characters; generate with `openssl rand -hex 32`. Required when `AUTH_ENABLED=true` | — |
| `SESSION_SECURE` | `Secure` flag on the session cookie; when `true` the cookie is named `__Host-session` | `true` |
| `SESSION_MAX_AGE_HOURS` | Absolute session lifetime, counted from login. After it the user signs in again, which re-checks `ALLOWED_GROUPS` | `12` |
| `TRUSTED_HOSTS` | Optional comma-separated list of accepted `Host` header values; other hosts get `400`. `/healthz` is exempt so probes keep working | — |
| `ENABLE_DEBUG_ENDPOINT` | Register `/api/debug/vsphere`, which lists every virtual disk visible to the vCenter account | `false` |

With `AUTH_ENABLED=true` (the default) the application refuses to start unless `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `SESSION_SECRET` are set. An empty `OIDC_ISSUER` does not disable login.

The login uses PKCE (S256), and the ID token's issuer and audience must match `OIDC_ISSUER` and `OIDC_CLIENT_ID`. Only the groups that match `ALLOWED_GROUPS` are stored in the session cookie. The cookie is signed, not stored server-side: logout clears it in the browser, and rotating `SESSION_SECRET` ends every session.

#### Dex client registration

Add a `staticClient` entry to your Dex configuration:

```yaml
staticClients:
  - id: pvc-monitor
    secret: <OIDC_CLIENT_SECRET>
    name: PVC Monitor
    redirectURIs:
      - https://pvc-monitor.example.com/auth/callback
```

For group-based access control, ensure your Dex connector is configured to return group membership (e.g. LDAP `groupSearch`, AD groups). The `groups` scope is requested by default.

## Unclaimed disks

The vSphere FCD catalogue on the scanned datastores is the source of what exists. A disk counts as **claimed** when any configured cluster has a `csi.vsphere.vmware.com` PV whose `volumeHandle` equals the disk's id (or an in-tree PV with the same file path). Released PVs still count as claims.

### Fail-closed behaviour

- If the PersistentVolume list of **any** configured cluster cannot be read completely, no unclaimed-disk result is produced. The API answers `503` with a message naming the cluster, the page shows it, and the alert records it as its last error. The detail goes to the server log.
- A datastore that cannot be listed, a disk that cannot be retrieved, a failed or partial VM scan and a failed CNS query are returned with the result as `problems`, with `complete: false`. The page shows a warning banner and the e-mail states it in the subject and at the top. An incomplete scan, or one with disks that could not be categorised (`unknown`), is never reported as "none found".
- If no datastore can be listed at all, the result is an error, not an empty list.

### Categories

Every disk that is not in use gets exactly one category:

| Category | Meaning | What to do |
|----------|---------|------------|
| `in_use` | Claimed by a configured cluster. Counted, not listed | Nothing |
| `inconsistent` | Claimed, but CNS does not know the disk, CNS records a different cluster-id than the cluster holding the PV, or more than one PV claims it | Check the CSI driver and CNS for that cluster. Never a removal candidate |
| `orphan_known_to_cns` | Not claimed; CNS knows it and records a configured cluster | Look in that cluster for the PV that was deleted (for example with reclaim policy `Retain`) |
| `foreign` | Not claimed; CNS records a cluster-id that is not configured | Probably another cluster's disk. Add that cluster to the configuration; never a candidate |
| `candidate` | Not claimed by any configured cluster and not known to CNS | Find the owner: a non-Kubernetes consumer, a static disk, or a leftover |
| `unknown` | CNS state unknown (query failed or disabled), CNS records no cluster-id, or the cluster-id cannot be placed because not every configured cluster has a `csi_cluster_id` | Fix the cause shown in the banner and scan again |

With `VCENTER_CNS_ENABLED=false` the CNS state is unknown for every disk, so every unclaimed disk is `unknown` and none needs investigation. Set `csi_cluster_id` for every cluster; without it, a CNS cluster-id cannot be told apart from another cluster's and the disk stays `unknown`. In-tree PVs that are not registered with CNS are treated as in use without the CNS check.

### Safeguards

Each listed disk carries safeguards that can only demote it:

- `attached`: `yes` / `no` / `unknown` from a scan of every VM's disks. It is `no` only after a complete VM scan; when the scan fails or a VM cannot be read it is `unknown`.
- `age_hours` from the disk's create time, and `too_young` below `UNCLAIMED_MIN_AGE_HOURS` (a disk can exist briefly before its PV). A disk without a readable create time has unknown age.
- `looks_like_csi`: the name follows the CSI pattern `pvc-<uuid>`. Informational only.

A disk **needs investigation** (is actionable) only when it is `candidate` or `orphan_known_to_cns`, `attached` is `no`, and its known age is at least the minimum. Only those disks trigger the alert e-mail. Nothing in this application establishes that a disk can be removed; the list is a starting point for investigation.

### Cloud Native Storage query: not tested against a live vCenter

The CNS query is one small function (`_query_cns_volumes` in `backend/vcenter_client.py`). Its method, data types, paging and privilege come from the type definitions shipped with pyVmomi 9.1.1.0 (`CnsQueryVolume` on `vim.cns.VolumeManager`, API version `vsan.version.version11`, privilege `Cns.Searchable`). The endpoint path (`/vsanHealth` on the vCenter host) and the managed object id (`cns-volume-manager`) are taken from govmomi's `cns` client; reusing the logged-in session cookie follows that client, but has not been tested. If CNS answers but knows none of the disks the configured clusters claim (for example an empty answer for lack of a privilege), the CNS state is treated as unknown rather than marking every unclaimed disk a candidate. The tests run the real pyVmomi binding against a fake transport only. Verify the result against the vSphere Client's Container Volumes view before relying on it. A CNS failure never hides the list: every disk's CNS state becomes unknown and the problem is shown.

## Running locally

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
cp .env.example .env          # fill in values
uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

`backend/.env` is read once at start, before any setting is used.

```bash
# Frontend (separate terminal)
cd frontend
npm install
npm run dev                   # proxies /api, /auth and /healthz to 127.0.0.1:8000
```

Open http://localhost:5173. The Vite dev server and the docker-compose services listen on `127.0.0.1` only.

To run without login during local development, set `AUTH_ENABLED=false` in `.env` and keep the backend bound to `127.0.0.1`. To test the login flow locally, keep `AUTH_ENABLED=true` and set the four required OIDC and session values; `SESSION_SECURE=true` works on `http://localhost` because browsers treat localhost as a secure origin.

## Running tests

```bash
cd backend
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
pytest
```

The tests use fakes for vCenter, Kubernetes, SMTP and the identity provider, ignore `backend/.env` and need no network access.

## Docker

```bash
# Build
docker build -t pvc-monitor .

# Run
# Local trial without login: publish on loopback only.
docker run -p 127.0.0.1:8000:8000 \
  -e VCENTER_HOST=vcenter.example.com \
  -e VCENTER_USER=svc-pvc-monitor@vsphere.local \
  -e VCENTER_PASSWORD=secret \
  -e AUTH_ENABLED=false \
  pvc-monitor
```

## Deploying to Kubernetes

```bash
# Put real credentials in a private values file (do not commit it) under `secret.data`,
# or create the Secret yourself and set `secret.existingSecret`.
helm upgrade --install pvc-monitor helm/pvc-monitor -n pvc-monitor --create-namespace \
  --set gateway.parentRef.name=<gateway> \
  -f my-values.yaml
```

Traffic is exposed through the Gateway API: the chart creates a `ListenerSet` (HTTPS listener for `gateway.hostname`, attached to the Gateway in `gateway.parentRef`), an `HTTPRoute` and a cert-manager `Certificate`. The Gateway must allow ListenerSets. Set `gateway.enabled=false` to skip all of these.

The deployment uses the pod's ServiceAccount token for in-cluster Kubernetes access. The ClusterRole grants read-only access to:

- `persistentvolumes`, `persistentvolumeclaims`
- `volumesnapshots`, `volumesnapshotcontents` (snapshot.storage.k8s.io)

Chart values for the clusters, the datastore allow-list and the other new settings:

```yaml
clusters:
  localName: local
  localCsiClusterId: cluster-a          # [Global] cluster-id of this cluster's CSI driver
  kubeconfigSecret: pvc-monitor-kubeconfigs   # existing Secret, one kubeconfig per key
  remote:
    - name: cluster-b
      kubeconfigKey: cluster-b
      context: ""                       # optional
      csiClusterId: cluster-b
secret:
  data:
    VCENTER_DATASTORES: "ds-k8s-01,ds-k8s-02"
    UNCLAIMED_MIN_AGE_HOURS: "24"
    VCENTER_CNS_ENABLED: "true"
```

The chart mounts the Secret read-only at `/etc/pvc-monitor/kubeconfigs` and sets `K8S_CLUSTERS` with the local cluster first. Without `clusters.remote` it sets `K8S_CLUSTER_NAME` and `K8S_CSI_CLUSTER_ID` for the single cluster. Create the Secret with, for example, `kubectl create secret generic pvc-monitor-kubeconfigs --from-file=cluster-b=./cluster-b.kubeconfig`. The remote clusters need the ClusterRole shown under [Several clusters](#several-clusters).

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | vCenter connectivity check |
| `GET` | `/api/persistent-volumes` | `{volumes, complete, problems}`: vSphere FCDs with K8s PV data |
| `GET` | `/api/container-volumes` | Kubernetes PVCs (CSI driver) |
| `GET` | `/api/snapshots` | FCD-level snapshots |
| `GET` | `/api/orphaned-fcds` | Disks not claimed by any configured cluster: `{disks, counts, complete, problems, cns, clusters, min_age_hours, scanned_at}`; `503` naming the cluster when a cluster cannot be read |
| `GET` | `/api/overview` | `{volumes, vsphere_error, vsphere_complete, vsphere_problems}`: PVC + FCD usage overview |
| `GET` | `/api/k8s/health` | Kubernetes API connectivity check |
| `GET` | `/api/k8s/persistent-volumes` | Raw K8s PersistentVolumes |
| `GET` | `/api/k8s/persistent-volume-claims` | Raw K8s PersistentVolumeClaims |
| `GET` | `/api/k8s/volume-snapshots` | K8s VolumeSnapshot objects |
| `GET` | `/api/k8s/volume-snapshot-contents` | K8s VolumeSnapshotContent objects |
| `GET` | `/api/alerts/status` | Alert scheduler state (snapshots + orphans) |
| `POST` | `/api/alerts/test` | Send a test snapshot age alert email |
| `POST` | `/api/alerts/orphan-test` | Send a test unclaimed-disk alert email |
| `GET` | `/api/debug/vsphere` | vSphere diagnostics; only when `ENABLE_DEBUG_ENDPOINT=true` |
| `GET` | `/auth/user` | Current session user info |
| `GET` | `/healthz` | Liveness probe, no external calls, no login |

The two `POST` routes refuse cross-site browser requests (`Sec-Fetch-Site` / `Origin` check) and answer `429` when the same kind was triggered less than a minute ago. Errors return a fixed message; the detail is written to the server log. The interactive API pages (`/docs`, `/redoc`, `/openapi.json`) are disabled.

Every response carries `Content-Security-Policy` (`default-src 'self'; frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and `X-Frame-Options: DENY`. The SPA is served from the same origin as the API, so no CORS headers are sent.

## Project structure

```
├── backend/
│   ├── main.py              # FastAPI app, all API endpoints
│   ├── settings.py          # Typed environment settings and start-up errors
│   ├── vcenter_client.py    # pyVmomi — FCD catalog, snapshots, VM attachment, CNS, unclaimed-disk classification
│   ├── k8s_client.py        # kubernetes-client — cluster configuration, PVs, PVCs, VolumeSnapshots
│   ├── auth.py              # OIDC middleware, session, routes and error pages
│   ├── alerting.py          # Email alert scheduler (snapshot age + unclaimed disks)
│   ├── conftest.py          # Shared test set-up (no .env, no network)
│   ├── test_*.py            # pytest tests
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── src/
│       ├── App.jsx
│       └── components/
│           ├── Overview.jsx
│           ├── PersistentVolumes.jsx
│           ├── ContainerVolumes.jsx
│           ├── Snapshots.jsx
│           ├── OrphanedFCDs.jsx     # Unclaimed Disks tab
│           ├── ScanWarning.jsx
│           ├── K8sVolumes.jsx
│           └── K8sSnapshots.jsx
├── helm/pvc-monitor/        # Helm chart (Deployment, RBAC, Secret, ListenerSet, HTTPRoute, ...)
└── Dockerfile
```
