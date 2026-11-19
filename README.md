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
| **Snapshot age** | Any FCD snapshot older than `ALERT_SNAPSHOT_AGE_DAYS` (default 7 days); an incomplete scan is stated in the subject and never reported as "no old snapshots" | `[PVC Monitor] N snapshots older than X days` |
| **Unclaimed disks** | Any disk that [needs investigation](#safeguards); other categories are listed as counts | `[PVC Monitor] N disks not claimed by any configured cluster need investigation` |

Each alert tab has a **Test Alert** button that sends a one-off email immediately, even when there is nothing to report.

When `ALERT_FAILURE_THRESHOLD` (default 3) scheduled runs of one alert kind in a row fail or are incomplete, one extra e-mail says that check is failing or incomplete, with the same fixed reason the UI shows. The next such e-mail is sent only after a successful run has reset the count. Test alerts do not count.

### Authentication

Login through an OIDC provider (for example Dex) is on by default, with group-based access control (`ALLOWED_GROUPS`, required; `*` admits every user the provider authenticates and logs a warning at start). The application refuses to start when login is enabled but not fully configured; running without login requires `AUTH_ENABLED=false`. When a user's groups do not intersect with the allowed set, they see a 403 page listing their groups and the required ones, right after the denied sign-in only. When the sign-in itself fails (for example a misconfigured issuer), a short error page with a retry link is shown instead of an automatic redirect to the login, which could loop; the detail is in the server log.

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

Built as a single Docker image (multi-stage): Node 26 builds the React app, Python 3.12 serves it via FastAPI/uvicorn.

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
4. Assign the built-in **Read-only** role to the account on **every VM that can use these datastores**, not only the Kubernetes node VMs (for example on the datacenter or the cluster, propagated). A disk attached to a VM the account cannot see would otherwise look unattached. The application also asks vSphere which VMs each unclaimed disk is associated with (`RetrieveVStorageObjectAssociations`, privilege `System.View` according to pyVmomi's type definitions). Whether that answer includes VMs the account cannot read has not been verified, so it does not replace read access on the VMs.

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
| `VCENTER_CACHE_SECONDS` | How long a finished vCenter scan is reused. Only one scan runs at a time; requests that arrive during a scan wait for it and get its result | `60` |
| `VCENTER_SCAN_WAIT_SECONDS` | How long a request waits for a running scan; after that it gets `503` "scan in progress" instead of queueing | `120` |

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
| `ALERT_FAILURE_THRESHOLD` | Failed or incomplete scheduled runs in a row (per alert kind) before one "check failing" e-mail (minimum `1`) | `3` |

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
| `OIDC_REDIRECT_URI` | Full callback URL; set it explicitly in production (the Helm chart sets `https://<gateway.hostname>/auth/callback` when empty) | derived from the request |
| `OIDC_SCOPES` | Requested scopes | `openid email profile groups` |
| `ALLOWED_GROUPS` | Comma-separated groups allowed to log in. Required when `AUTH_ENABLED=true`; `*` allows every authenticated user and logs a warning at start | — |
| `SESSION_SECRET` | Cookie signing key, at least 32 characters; generate with `openssl rand -hex 32`. Required when `AUTH_ENABLED=true` | — |
| `SESSION_SECURE` | `Secure` flag on the session cookie; when `true` the cookie is named `__Host-session` | `true` |
| `SESSION_MAX_AGE_HOURS` | Absolute session lifetime, counted from login. After it the user signs in again, which re-checks `ALLOWED_GROUPS` | `12` |
| `TRUSTED_HOSTS` | Optional comma-separated list of accepted `Host` header values; other hosts get `400`. `/healthz` is exempt so probes keep working. The Helm chart sets `gateway.hostname` when empty | — |
| `FORWARDED_ALLOW_IPS` | Read by uvicorn: addresses (IPs or CIDRs) whose `X-Forwarded-*` headers are trusted. Set it to the reverse proxy or Gateway data plane so the app sees the original `https` scheme and client address; the Helm chart sets it from `forwardedAllowIps` | `127.0.0.1` |
| `ENABLE_DEBUG_ENDPOINT` | Register `/api/debug/vsphere`, which lists every virtual disk visible to the vCenter account | `false` |

With `AUTH_ENABLED=true` (the default) the application refuses to start unless `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SESSION_SECRET` and `ALLOWED_GROUPS` are set. An empty `OIDC_ISSUER` does not disable login. `AUTH_ENABLED` is false for `false`, `0`, `no` or `off` in any case; the Helm chart recognises the same values.

The login uses PKCE (S256), and the ID token's issuer and audience must match `OIDC_ISSUER` and `OIDC_CLIENT_ID`. Only the groups that match `ALLOWED_GROUPS` are stored in the session cookie. The cookie is signed, not stored server-side: logout (a same-origin `POST /auth/logout`) clears it in the browser, and rotating `SESSION_SECRET` ends every session.

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
- A datastore that cannot be listed, a disk that cannot be retrieved, a failed or partial VM scan (including a VM whose configuration cannot be read), disks whose VM associations vSphere could not report, a failed or unvalidated CNS answer, and in-tree PVs that match no disk are returned with the result as `problems`, with `complete: false`. The page shows a warning banner and the e-mail states it in the subject and at the top. An incomplete scan, or one with disks that could not be categorised (`unknown`), is never reported as "none found".
- If no datastore can be listed at all, the result is an error, not an empty list.
- If any configured cluster has `kubernetes.io/vsphere-volume` PVs whose `volumePath` matches no disk on the scanned datastores, the problem names the cluster and the count, and every unclaimed disk becomes `unknown` for that scan: such a PV may claim a disk the path match cannot see. In-tree volumes on datastores outside `VCENTER_DATASTORES`, or that are not registered as First Class Disks, trigger this on every scan.

### Categories

Every disk that is not in use gets exactly one category:

| Category | Meaning | What to do |
|----------|---------|------------|
| `in_use` | Claimed by a configured cluster. Counted, not listed | Nothing |
| `inconsistent` | Claimed, but CNS does not know the disk, CNS records a different cluster-id than the cluster holding the PV, or more than one PV claims it | Check the CSI driver and CNS for that cluster. Never a removal candidate |
| `orphan_known_to_cns` | Not claimed; CNS knows it and records a configured cluster | Look in that cluster for the PV that was deleted (for example with reclaim policy `Retain`) |
| `foreign` | Not claimed; CNS records a cluster-id that is not configured | Probably another cluster's disk. Add that cluster to the configuration; never a candidate |
| `candidate` | Not claimed by any configured cluster and not known to CNS. Actionable only when the name follows the CSI pattern (`looks_like_csi`); other candidates are listed but never need investigation or trigger an alert | Find the owner: a non-Kubernetes consumer, a static disk, or a leftover |
| `unknown` | CNS state unknown (query failed, disabled or not validated, see below), CNS records no cluster-id, the cluster-id cannot be placed because not every configured cluster has a `csi_cluster_id`, or in-tree PVs could not be matched | Fix the cause shown in the banner and scan again |

A CNS answer is used only when it is validated: it must contain volumes, the scanned datastores must hold at least one disk claimed by a CSI PV, and CNS must know at least one of those claimed disks. Otherwise the CNS state is unknown for every disk and the reason is recorded as a problem. With `VCENTER_CNS_ENABLED=false` the CNS state is unknown for every disk, so every unclaimed disk is `unknown` and none needs investigation. Set `csi_cluster_id` for every cluster; without it, a CNS cluster-id cannot be told apart from another cluster's and the disk stays `unknown`. In-tree PVs that are not registered with CNS are treated as in use without the CNS check.

### Safeguards

Each listed disk carries safeguards that can only demote it:

- `attached`: `yes` / `no` / `unknown` from two sources: a scan of every VM's disks, and vSphere's own list of VMs associated with each unclaimed disk (`RetrieveVStorageObjectAssociations`, asked in batches). It is `yes` when either source names a VM, `no` only when the VM scan was complete **and** vSphere answered for that disk without any VM, and `unknown` otherwise (scan failed, a VM's configuration could not be read, the association call failed or returned a fault or no entry for the disk). The Persistent Volumes and Overview views use the VM scan only: there `no` (shown as idle) means a complete VM scan found no VM.
- `age_hours` from the disk's create time, and `too_young` below `UNCLAIMED_MIN_AGE_HOURS` (a disk can exist briefly before its PV). A disk without a readable create time has unknown age.
- `looks_like_csi`: the name follows the CSI pattern `pvc-<uuid>`. Required for a `candidate` to be actionable.

A disk **needs investigation** (is actionable) only when it is `orphan_known_to_cns`, or `candidate` with a CSI name, `attached` is `no`, and its known age is at least the minimum. Only those disks trigger the alert e-mail. Nothing in this application establishes that a disk can be removed; the list is a starting point for investigation.

### Cloud Native Storage query: not tested against a live vCenter

The CNS query is one small function (`_query_cns_volumes` in `backend/vcenter_client.py`). Its method, data types, paging and privilege come from the type definitions shipped with pyVmomi 9.1.1.0 (`CnsQueryVolume` on `vim.cns.VolumeManager`, API version `vsan.version.version11`, privilege `Cns.Searchable`). The endpoint path (`/vsanHealth` on the vCenter host) and the managed object id (`cns-volume-manager`) are taken from govmomi's `cns` client; reusing the logged-in session cookie follows that client, but has not been tested. If the CNS answer cannot be validated against a claimed disk (for example an empty answer for lack of a privilege), the CNS state is treated as unknown rather than marking every unclaimed disk a candidate. The tests run the real pyVmomi binding against a fake transport only. Verify the result against the vSphere Client's Container Volumes view before relying on it. A CNS failure never hides the list: every disk's CNS state becomes unknown and the problem is shown.

The VM association query (`_query_associations`) is not tested against a live vCenter either: its method, argument and result types and privilege come from pyVmomi 9.1.1.0 (`RetrieveVStorageObjectAssociations` on `vim.vslm.vcenter.VStorageObjectManager`, since `vim.version.version12`, privilege `System.View`). Whether vCenter returns an entry without associations for a disk no VM uses has not been verified; a disk without an entry is treated as `unknown`, never as unattached.

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

Open http://localhost:3000. The Vite dev server and the docker-compose services listen on `127.0.0.1` only.

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

With `gateway.enabled` and the chart's own Secret, the Deployment sets `TRUSTED_HOSTS` to `gateway.hostname` and `OIDC_REDIRECT_URI` to `https://<gateway.hostname>/auth/callback` unless `secret.data` sets them. With `secret.existingSecret` the chart cannot see your values and sets neither; put both in that Secret. Also set `forwardedAllowIps` to the Gateway data-plane addresses (it becomes `FORWARDED_ALLOW_IPS`); otherwise uvicorn ignores the `X-Forwarded-*` headers and the app does not see the original `https` scheme. The chart refuses to render an exposed release with `AUTH_ENABLED` set to any false spelling (`false`, `0`, `no`, `off`, any case), or with authentication on and `OIDC_*`, `SESSION_SECRET` or `ALLOWED_GROUPS` empty.

The chart mounts the Secret read-only at `/etc/pvc-monitor/kubeconfigs` and sets `K8S_CLUSTERS` with the local cluster first. Without `clusters.remote` it sets `K8S_CLUSTER_NAME` and `K8S_CSI_CLUSTER_ID` for the single cluster. Create the Secret with, for example, `kubectl create secret generic pvc-monitor-kubeconfigs --from-file=cluster-b=./cluster-b.kubeconfig`. The remote clusters need the ClusterRole shown under [Several clusters](#several-clusters).

## API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | vCenter connectivity check |
| `GET` | `/api/persistent-volumes` | `{volumes, complete, problems}`: vSphere FCDs with K8s PV data |
| `GET` | `/api/container-volumes` | Kubernetes PVCs (CSI driver) |
| `GET` | `/api/snapshots` | `{snapshots, complete, problems}`: FCD-level snapshots |
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
| `POST` | `/auth/logout` | End the session; same-origin check as the alert routes, answers `303` to the login |
| `GET` | `/healthz` | Liveness probe, no external calls, no login |

The `POST` routes refuse cross-site browser requests (`Sec-Fetch-Site` / `Origin` check); the two alert routes also answer `429` when the same kind was triggered less than a minute ago. Errors return a fixed message; the detail is written to the server log. Routes that scan vCenter answer `503` "scan in progress" when another scan holds the lock longer than `VCENTER_SCAN_WAIT_SECONDS`. The interactive API pages (`/docs`, `/redoc`, `/openapi.json`) are disabled.

Every response from the application, including unhandled `500` errors, carries `Content-Security-Policy` (`default-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and `X-Frame-Options: DENY`. The sign-in error and 403 pages send their own, stricter policy that allows only their inline style. In the docker-compose set-up, nginx sends the same policy for the SPA it serves. The SPA is served from the same origin as the API, so no CORS headers are sent.

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
