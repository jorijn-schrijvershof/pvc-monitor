import functools
import logging
import os
import re
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import SoapStubAdapter, vim

from settings import Unavailable, env_bool, env_float, env_int, env_list, env_str

logger = logging.getLogger(__name__)

# Matches snapshot delta disks: basename-000001.vmdk, basename-0001.vmdk, etc.
_SNAPSHOT_DELTA_RE = re.compile(r'-\d{4,6}\.vmdk$', re.IGNORECASE)


def _delta_to_base(file_path: str) -> str | None:
    """
    If file_path is a snapshot delta VMDK (ends in -NNNNNN.vmdk), return the
    base path (ends in .vmdk).  Otherwise return None.

    Example: "[ds] fcd/_x/f799081d951647d58572810601d0d2b8-000001.vmdk"
          -> "[ds] fcd/_x/f799081d951647d58572810601d0d2b8.vmdk"
    """
    if _SNAPSHOT_DELTA_RE.search(file_path):
        return _SNAPSHOT_DELTA_RE.sub('.vmdk', file_path)
    return None


def _nodash(uuid: str) -> str:
    """Return a UUID string with all dashes removed.

    vSphere stores FCDs with UUID-without-dashes as the backing filename:
      volumeHandle  f799081d-9516-47d5-8572-810601d0d2b8   (K8s, with dashes)
      VMDK filename f799081d951647d58572810601d0d2b8.vmdk  (vSphere, no dashes)
    Substring-matching the no-dash form finds both the base disk and any delta.
    """
    return uuid.replace('-', '')


VCENTER_HOST = env_str("VCENTER_HOST")
VCENTER_USER = env_str("VCENTER_USER")
VCENTER_PASSWORD = os.getenv("VCENTER_PASSWORD", "")  # used as given
VCENTER_PORT = env_int("VCENTER_PORT", 443, minimum=1)
VCENTER_VERIFY_SSL = env_bool("VCENTER_VERIFY_SSL", True)
VCENTER_CA_FILE = env_str("VCENTER_CA_FILE") or None  # PEM bundle for a private vCenter CA
# How long a finished vCenter scan is reused by later callers.
VCENTER_CACHE_SECONDS = env_int("VCENTER_CACHE_SECONDS", 60, minimum=0)
# Socket timeout for each request to vCenter.
VCENTER_TIMEOUT_SECONDS = 60

if not VCENTER_VERIFY_SSL:
    logger.warning(
        "vCenter: VCENTER_VERIFY_SSL=false, the vCenter certificate is NOT verified "
        "and the service-account password can be intercepted"
    )


def _ssl_context() -> ssl.SSLContext:
    # create_default_context: certificate required, hostname checked, system
    # trust store (or VCENTER_CA_FILE when set).
    context = ssl.create_default_context(cafile=VCENTER_CA_FILE)
    if not VCENTER_VERIFY_SSL:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _get_connection():
    if not VCENTER_HOST:
        raise Unavailable("vCenter is not configured")
    return SmartConnect(
        host=VCENTER_HOST,
        user=VCENTER_USER,
        pwd=VCENTER_PASSWORD,
        port=VCENTER_PORT,
        sslContext=_ssl_context(),
        httpConnectionTimeout=VCENTER_TIMEOUT_SECONDS,
    )


@contextmanager
def _session():
    """A logged-in vCenter session that is always logged out afterwards."""
    si = _get_connection()
    try:
        yield si
    finally:
        try:
            Disconnect(si)
        except Exception:
            logger.warning("vCenter: logout failed", exc_info=True)


@contextmanager
def _container_view(content, obj_type):
    """Objects of one type under rootFolder; the view is always destroyed."""
    view = content.viewManager.CreateContainerView(content.rootFolder, [obj_type], True)
    try:
        yield view.view
    finally:
        try:
            view.Destroy()
        except Exception:
            pass


# At most one vCenter call runs at a time; callers that waited for it get the
# result of the call that just finished instead of starting another one.
# One process-wide lock keeps this simple; use per-call locks if scans must overlap.
_scan_lock = threading.Lock()
_scan_cache: dict = {}


def _single_flight(fn):
    @functools.wraps(fn)
    def wrapper():
        with _scan_lock:
            hit = _scan_cache.get(fn.__name__)
            if hit and time.monotonic() - hit[0] < VCENTER_CACHE_SECONDS:
                return hit[1]
            result = fn()  # failures are not cached
            _scan_cache[fn.__name__] = (time.monotonic(), result)
            return result
    return wrapper


@_single_flight
def check_connectivity() -> dict:
    try:
        with _session() as si:
            about = si.RetrieveContent().about
            return {
                "connected": True,
                "vcenter_host": VCENTER_HOST,
                "version": about.version,
                "build": about.build,
                "full_name": about.fullName,
            }
    except Unavailable as exc:
        error = str(exc)
    except Exception:
        logger.exception("vCenter: connectivity check failed")
        error = "connection failed, see the server log"
    return {"connected": False, "vcenter_host": VCENTER_HOST, "error": error}


VSPHERE_CSI_DRIVER = "csi.vsphere.vmware.com"
IN_TREE_DRIVER = "kubernetes.io/vsphere-volume"

# Datastore names to scan; empty scans every datastore the account can see.
VCENTER_DATASTORES = env_list("VCENTER_DATASTORES")
# Ask Cloud Native Storage which cluster each disk belongs to.
VCENTER_CNS_ENABLED = env_bool("VCENTER_CNS_ENABLED", True)
# A disk younger than this is never actionable: its PV may not exist yet.
UNCLAIMED_MIN_AGE_HOURS = env_float("UNCLAIMED_MIN_AGE_HOURS", 24, minimum=0)
CNS_PAGE_SIZE = 500

# Name the vSphere CSI driver gives a dynamically provisioned disk (its PV name).
_CSI_NAME_RE = re.compile(r"^pvc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

_VM_SCAN_PROBLEM = "The VM scan failed or was incomplete; attachment is unknown for disks not found on a VM"
_CNS_PROBLEM = "Cloud Native Storage could not be queried; CNS state is unknown for every disk"
_CNS_EMPTY_PROBLEM = ("Cloud Native Storage knows none of the disks that configured clusters claim; "
                      "CNS state is treated as unknown for every disk")


# ── Kubernetes claims ────────────────────────────────────────────────────────

def _claims() -> tuple[dict, dict]:
    """PVs of all configured clusters that can claim an FCD, keyed for lookup.

    by_handle: CSI volumeHandle (lower) -> PVs; the handle is the FCD id.
    by_path:   in-tree vsphereVolume volumePath (lower) -> PVs; compared with
               the FCD backing file path.
    Raises (naming the cluster) when any cluster's PV list is incomplete.
    """
    import k8s_client
    by_handle: dict = {}
    by_path: dict = {}
    for pv in k8s_client.get_pvs_of_all_clusters():
        key = (pv.get("volume_handle") or "").strip().lower()
        if not key:
            continue
        if pv.get("driver") == VSPHERE_CSI_DRIVER:
            by_handle.setdefault(key, []).append(pv)
        elif pv.get("driver") == IN_TREE_DRIVER:
            by_path.setdefault(key, []).append(pv)
    return by_handle, by_path


def _claiming_pvs(fcd: dict, claims: tuple[dict, dict]) -> list[dict]:
    """PVs that claim this FCD: by UUID, or for in-tree PVs by exact file path. Never by name."""
    by_handle, by_path = claims
    return by_handle.get(fcd["id"], []) + by_path.get(fcd["file_path"].strip().lower(), [])


# ── vCenter inventory ────────────────────────────────────────────────────────

def _ds_label(ds) -> str:
    """Safe datastore label that never triggers a permission-gated property fetch."""
    try:
        return ds.name
    except Exception:
        try:
            return ds._moId
        except Exception:
            return str(ds)


def _build_vm_attachment_index(content) -> tuple[dict, dict, bool]:
    """
    Scan every VM and return (obj_id_index, filepath_index, complete):
        obj_id_index   – backingObjectId (lower) → vm_info
        filepath_index – fileName (lower)        → vm_info
        complete       – False when the scan or any VM could not be read

    filepath_index is keyed by BOTH the actual fileName AND (when the disk is a
    snapshot delta) the derived base path, so FCD catalog entries can match VMs
    that currently have a snapshot active on a CSI volume.
    """
    obj_id_index: dict = {}
    filepath_index: dict = {}
    complete = True
    try:
        with _container_view(content, vim.VirtualMachine) as vm_view:
            for vm in vm_view:
                try:
                    if not vm.config:
                        continue
                    for device in vm.config.hardware.device:
                        if not isinstance(device, vim.vm.device.VirtualDisk):
                            continue
                        backing = device.backing
                        ds_ref = getattr(backing, "datastore", None)
                        vm_info = {
                            "vm_name": vm.name,
                            "vm_power_state": str(vm.runtime.powerState),
                            "file_path": getattr(backing, "fileName", "") or "N/A",
                            "datastore": ds_ref.name if ds_ref else "N/A",
                            "capacity_gb": round(device.capacityInKB / 1024 / 1024, 2),
                        }
                        obj_id = (getattr(backing, "backingObjectId", None) or "").lower()
                        if obj_id:
                            obj_id_index[obj_id] = vm_info
                        fp = (getattr(backing, "fileName", None) or "").lower()
                        if fp:
                            filepath_index[fp] = vm_info
                            # When the active disk is a snapshot delta, also index
                            # by the base path so the FCD catalog lookup still hits.
                            base = _delta_to_base(fp)
                            if base:
                                filepath_index[base] = vm_info
                except Exception:
                    logger.warning("vCenter: reading the disks of a VM failed", exc_info=True)
                    complete = False
    except Exception:
        logger.exception("vCenter: the VM scan failed")
        complete = False
    return obj_id_index, filepath_index, complete


def _attachment(fcd: dict, vm_index) -> tuple[dict | None, str]:
    """(vm_info, "yes" | "no" | "unknown"). "no" only after a complete VM scan."""
    obj_ids, paths, complete = vm_index
    vm = obj_ids.get(fcd["id"]) or obj_ids.get(_nodash(fcd["id"])) or paths.get(fcd["file_path"].lower())
    if vm:
        return vm, "yes"
    return None, "no" if complete else "unknown"


def _log_list_failure(ds_name: str, exc: Exception) -> None:
    if isinstance(exc, vim.fault.NoPermission):
        logger.error(
            "vCenter: the account may not list First Class Disks on datastore %r. "
            "ListVStorageObject needs Datastore.FileManagement (Low level file operations) "
            "on the datastore; see 'Required vCenter privileges' in the README", ds_name,
        )
    else:
        logger.warning("vCenter: listing First Class Disks on datastore %r failed", ds_name, exc_info=True)


def _scan_catalog(content) -> tuple[list[dict], list[str]]:
    """Every FCD on the scanned datastores, plus a list of problems.

    The catalogue is the source of what exists. A datastore that cannot be
    listed and an FCD that cannot be retrieved are recorded as problems, never
    skipped silently. Raises Unavailable when no datastore could be listed at all.
    """
    problems: list[str] = []
    fcds: list[dict] = []
    vsom = content.vStorageObjectManager
    with _container_view(content, vim.Datastore) as ds_view:
        datastores = [(_ds_label(ds), ds) for ds in ds_view]
    if VCENTER_DATASTORES:
        found = {name for name, _ in datastores}
        problems += [f"Datastore {name} from VCENTER_DATASTORES was not found"
                     for name in VCENTER_DATASTORES if name not in found]
        datastores = [(name, ds) for name, ds in datastores if name in VCENTER_DATASTORES]

    listed = 0
    for ds_name, ds in datastores:
        try:
            refs = vsom.ListVStorageObject(ds)
        except Exception as exc:
            _log_list_failure(ds_name, exc)
            problems.append(f"Datastore {ds_name}: First Class Disks could not be listed")
            continue
        listed += 1
        for ref in refs or []:
            ref_id = getattr(ref, "id", ref)
            try:
                cfg = vsom.RetrieveVStorageObject(ref, ds).config
                backing = getattr(cfg, "backing", None)
                fcds.append({
                    "id": (cfg.id.id or "").lower(),
                    "fcd_id": cfg.id.id,
                    "name": getattr(cfg, "name", "") or "",
                    "datastore": ds_name,
                    "capacity_gb": round((getattr(cfg, "capacityInMB", 0) or 0) / 1024, 2),
                    "file_path": (getattr(backing, "filePath", "") if backing else "") or "",
                    "created": getattr(cfg, "createTime", None),
                    "ref": ref,
                    "ds": ds,
                })
            except Exception:
                logger.warning("vCenter: retrieving disk %s on datastore %r failed", ref_id, ds_name, exc_info=True)
                problems.append(f"Datastore {ds_name}: disk {ref_id} could not be retrieved")

    if not listed:
        raise Unavailable(
            "No datastore could be listed in vCenter, so no result is produced. Check the account's "
            "Datastore.FileManagement privilege and VCENTER_DATASTORES; see the server log."
        )
    return fcds, problems


# ── Cloud Native Storage ─────────────────────────────────────────────────────

def _query_cns_volumes(si) -> dict[str, str]:
    """FCD id (lower) -> CNS container cluster-id ("" when CNS has none), for every
    volume Cloud Native Storage knows.

    NOT TESTED AGAINST A LIVE vCENTER. The method, types, paging and privilege come
    from the pyVmomi 9.1.1.0 type definitions (_typeinfo_vsanhealth.py):
    vim.cns.VolumeManager.Query is CnsQueryVolume (since vsan.version.version11,
    privilege Cns.Searchable), paged with vim.cns.QueryFilter.cursor. pyVmomi does
    not say where the service lives: the /vsanHealth path and the managed object id
    "cns-volume-manager" are taken from govmomi's cns/client.go, which reaches the
    service through the logged-in vCenter session; reusing the session cookie here
    follows that, but is not tested. govmomi asks for the vCenter's current vSAN
    API version; this uses version11, the lowest that has Query. Read-only: only
    Query is called.
    """
    stub = SoapStubAdapter(
        host=VCENTER_HOST,
        port=VCENTER_PORT,
        path="/vsanHealth",
        version="vsan.version.version11",  # the lowest version that has Query
        sslContext=_ssl_context(),
        httpConnectionTimeout=VCENTER_TIMEOUT_SECONDS,
    )
    stub.cookie = si._stub.cookie  # the logged-in vCenter session
    try:
        manager = vim.cns.VolumeManager("cns-volume-manager", stub)
        volumes: dict[str, str] = {}
        offset = 0
        while True:
            page = manager.Query(vim.cns.QueryFilter(cursor=vim.cns.Cursor(offset=offset, limit=CNS_PAGE_SIZE)))
            for vol in page.volumes or []:
                cluster = getattr(getattr(vol, "metadata", None), "containerCluster", None)
                volumes[vol.volumeId.id.lower()] = (getattr(cluster, "clusterId", None) or "").strip()
            total = page.cursor.totalRecords
            if not page.volumes or (total is not None and page.cursor.offset >= total):
                return volumes
            if page.cursor.offset <= offset:
                raise RuntimeError("CNS query cursor did not advance")
            offset = page.cursor.offset
    finally:
        stub.DropConnections()


# ── Classification ───────────────────────────────────────────────────────────

CATEGORIES = ("inconsistent", "orphan_known_to_cns", "foreign", "candidate", "unknown")
ACTIONABLE_CATEGORIES = ("candidate", "orphan_known_to_cns")


def classify(claims: list[tuple[str, bool]], cns, clusters) -> tuple[str, str]:
    """Category and reason for one FCD. Pure; uncertainty never promotes a disk.

    claims:   (cluster name, is CSI claim) for every PV that claims the disk
    cns:      None = CNS state unknown, False = not known to CNS,
              str = the CNS cluster-id ("" when CNS records none)
    clusters: the configured clusters (name, csi_cluster_id)
    """
    ids = {c.csi_cluster_id: c.name for c in clusters if c.csi_cluster_id}
    if claims:
        if len(claims) > 1:
            return "inconsistent", "claimed by more than one PV"
        holder, is_csi = claims[0]
        if cns is None or not is_csi:
            return "in_use", ""
        if cns is False:
            return "inconsistent", f"claimed in cluster {holder} but not known to CNS"
        holder_id = next((c.csi_cluster_id for c in clusters if c.name == holder), None)
        if holder_id and cns and cns != holder_id:
            return "inconsistent", f"claimed in cluster {holder} but CNS records cluster-id {cns}"
        return "in_use", ""
    if cns is None:
        return "unknown", "CNS state unknown"
    if cns is False:
        return "candidate", "not claimed by any configured cluster and not known to CNS"
    if cns in ids:
        return "orphan_known_to_cns", f"CNS records configured cluster {ids[cns]}, which has no PV for it"
    if cns and all(c.csi_cluster_id for c in clusters):
        return "foreign", f"CNS records cluster-id {cns}, which is not a configured cluster"
    return "unknown", "CNS cluster-id cannot be matched (not every configured cluster has a csi_cluster_id)"


def _age_hours(created, now: datetime) -> float | None:
    if not isinstance(created, datetime) or created.tzinfo is None:
        return None
    return round((now - created).total_seconds() / 3600, 1)


# ── Public scans ─────────────────────────────────────────────────────────────

@_single_flight
def get_persistent_volumes() -> dict:
    """FCDs claimed by a PV of any configured cluster, matched by UUID only."""
    claims = _claims()
    if not any(claims):
        return {"volumes": [], "complete": True, "problems": []}

    with _session() as si:
        content = si.RetrieveContent()
        vm_index = _build_vm_attachment_index(content)
        fcds, problems = _scan_catalog(content)
    if not vm_index[2]:
        problems.append(_VM_SCAN_PROBLEM)

    volumes = []
    for fcd in fcds:
        for pv in _claiming_pvs(fcd, claims):
            vm, attached = _attachment(fcd, vm_index)
            volumes.append({
                "cluster": pv["cluster"],
                "volume_id": fcd["fcd_id"],
                "pv_name": pv["name"],
                "claim": pv.get("claim_ref") or "—",
                "capacity_gb": fcd["capacity_gb"],
                "datastore": fcd["datastore"],
                "file_path": fcd["file_path"] or "N/A",
                "attached": attached,
                "vm_name": (vm or {}).get("vm_name") or "—",
                "vm_power_state": (vm or {}).get("vm_power_state") or "—",
                "storage_class": pv.get("storage_class") or "—",
                "k8s_status": pv.get("status") or "—",
            })
    return {"volumes": volumes, "complete": not problems, "problems": problems}


@_single_flight
def debug_vsphere_inventory() -> dict:
    """
    Diagnostic: all VMDKs on VMs, all accessible FCDs, and K8s PV search terms.
    GET /api/debug/vsphere
    """
    import k8s_client
    pvs = k8s_client.get_k8s_persistent_volumes()
    vsphere_pvs = []
    for p in pvs:
        if p.get("driver") != VSPHERE_CSI_DRIVER:
            continue
        claim_ref = p.get("claim_ref") or ""
        pvc_name = claim_ref.split("/")[-1] if "/" in claim_ref else ""
        vsphere_pvs.append({
            "pv_name": p["name"],
            "pvc_name": pvc_name,
            "volume_handle": p.get("volume_handle"),
            "status": p.get("status"),
        })

    with _session() as si:
        content = si.RetrieveContent()

        all_vmdks = []
        try:
            with _container_view(content, vim.VirtualMachine) as vm_view:
                for vm in vm_view:
                    try:
                        if not vm.config:
                            continue
                        for device in vm.config.hardware.device:
                            if not isinstance(device, vim.vm.device.VirtualDisk):
                                continue
                            all_vmdks.append({
                                "vm": vm.name,
                                "label": device.deviceInfo.label,
                                "file_path": getattr(device.backing, "fileName", ""),
                                "backing_obj_id": getattr(device.backing, "backingObjectId", None),
                                "capacity_gb": round(device.capacityInKB / 1024 / 1024, 2),
                            })
                    except Exception:
                        continue
        except Exception:
            pass

        all_fcds = []
        fcd_errors = []
        try:
            vsom = content.vStorageObjectManager
            with _container_view(content, vim.Datastore) as ds_view:
                for ds in ds_view:
                    ds_id = _ds_label(ds)
                    try:
                        refs = vsom.ListVStorageObject(ds)
                    except Exception as e:
                        fcd_errors.append(f"ListVStorageObject on {ds_id}: {type(e).__name__}")
                        continue
                    for ref in refs:
                        try:
                            obj = vsom.RetrieveVStorageObject(ref, ds)
                            cfg = obj.config
                            backing = getattr(cfg, "backing", None)
                            all_fcds.append({
                                "fcd_id": cfg.id.id,
                                "fcd_name": getattr(cfg, "name", None),
                                "datastore": ds_id,
                                "capacity_mb": getattr(cfg, "capacityInMB", 0),
                                "file_path": getattr(backing, "filePath", None) if backing else None,
                            })
                        except Exception as e:
                            fcd_errors.append(f"RetrieveVStorageObject on {ds_id}: {type(e).__name__}")
        except Exception as e:
            fcd_errors.append(f"vStorageObjectManager: {type(e).__name__}")

        return {
            "k8s_vsphere_pvs": vsphere_pvs,
            "vsphere_vmdks": all_vmdks,
            "vsphere_fcds": all_fcds,
            "fcd_errors": fcd_errors,
        }


@_single_flight
def get_snapshots() -> list[dict]:
    """
    Return FCD-level snapshots for vSphere CSI volumes.

    Kubernetes VolumeSnapshots backed by csi.vsphere.vmware.com are implemented
    as FCD snapshots, NOT VM snapshots.  The delta VMDKs are stored under the
    fcd/ directory on the datastore (e.g. fcd/_003c/<uuid>-000001.vmdk) and
    vm.snapshot on node VMs remains None.

    The correct vSphere API is vsom.RetrieveSnapshotInfo(fcd_id, datastore).
    Disks are matched to PVs by UUID only.
    """
    claims = _claims()
    if not any(claims):
        return []

    with _session() as si:
        content = si.RetrieveContent()
        fcds, problems = _scan_catalog(content)
        vsom = content.vStorageObjectManager
        all_snapshots = []
        for fcd in fcds:
            pvs = _claiming_pvs(fcd, claims)
            if not pvs:
                continue
            try:
                snap_info = vsom.RetrieveSnapshotInfo(fcd["ref"], fcd["ds"])
            except Exception:
                logger.warning("vCenter: reading snapshots of disk %s failed", fcd["fcd_id"], exc_info=True)
                problems.append(f"Disk {fcd['fcd_id']}: snapshots could not be read")
                continue
            for snap in getattr(snap_info, "snapshots", None) or []:
                try:
                    snap_id = snap.id.id if hasattr(snap.id, "id") else str(snap.id)
                except Exception:
                    snap_id = "—"
                try:
                    created = snap.createTime.isoformat() if getattr(snap, "createTime", None) else None
                except Exception:
                    created = None
                for pv in pvs:
                    claim_ref = pv.get("claim_ref") or ""
                    all_snapshots.append({
                        "cluster":       pv["cluster"],
                        "pv_name":       pv["name"],
                        "pvc_name":      claim_ref.split("/")[-1] if claim_ref else "—",
                        "volume_handle": pv.get("volume_handle") or "—",
                        "snapshot_id":   snap_id,
                        "description":   getattr(snap, "description", "") or "",
                        "created":       created,
                        "datastore":     fcd["datastore"],
                        "storage_class": pv.get("storage_class") or "—",
                    })
    # Snapshot scan problems are logged only; the view and the snapshot alert get the list.
    for problem in problems:
        logger.warning("vCenter: snapshot scan incomplete: %s", problem)
    return all_snapshots


@_single_flight
def get_orphaned_fcds() -> dict:
    """
    FCDs that no configured cluster claims, each with a category and safeguards.

    Fails closed: if the PV list of any configured cluster cannot be read,
    raise instead of producing a result. Gaps on the vSphere side (datastores,
    disks, VM scan, CNS) are returned as problems with complete=False.
    """
    import k8s_client
    clusters = k8s_client.CLUSTERS
    claims = _claims()

    with _session() as si:
        content = si.RetrieveContent()
        vm_index = _build_vm_attachment_index(content)
        fcds, problems = _scan_catalog(content)
        cns = None
        cns_state = "disabled"
        if VCENTER_CNS_ENABLED:
            try:
                cns = _query_cns_volumes(si)
                cns_state = "ok"
            except Exception:
                logger.exception("vCenter: the Cloud Native Storage query failed")
                cns_state = "failed"
                problems.append(_CNS_PROBLEM)
    if not vm_index[2]:
        problems.append(_VM_SCAN_PROBLEM)

    # An answer that knows none of the CSI-claimed disks (for example an empty
    # result for lack of a privilege) would turn every unclaimed disk into a
    # candidate. Treat it as unknown instead.
    if cns is not None:
        csi_claimed = [f for f in fcds if f["id"] in claims[0]]
        if csi_claimed and not any(f["id"] in cns for f in csi_claimed):
            logger.warning("vCenter: CNS knows none of the %d claimed disks; treating CNS state as unknown", len(csi_claimed))
            cns, cns_state = None, "failed"
            problems.append(_CNS_EMPTY_PROBLEM)

    now = datetime.now(timezone.utc)
    counts = {key: 0 for key in ("in_use", *CATEGORIES, "actionable")}
    disks = []
    for fcd in fcds:
        claiming = _claiming_pvs(fcd, claims)
        cns_id = None if cns is None else cns.get(fcd["id"], False)
        category, reason = classify(
            [(pv["cluster"], pv.get("driver") == VSPHERE_CSI_DRIVER) for pv in claiming], cns_id, clusters,
        )
        counts[category] += 1
        if category == "in_use":
            continue
        vm, attached = _attachment(fcd, vm_index)
        age = _age_hours(fcd["created"], now)
        too_young = None if age is None else age < UNCLAIMED_MIN_AGE_HOURS
        actionable = (
            category in ACTIONABLE_CATEGORIES and attached == "no" and too_young is False
        )
        counts["actionable"] += actionable
        cns_cluster = next((c.name for c in clusters if cns_id and c.csi_cluster_id == cns_id), None)
        disks.append({
            "fcd_id":         fcd["fcd_id"],
            "fcd_name":       fcd["name"] or "—",
            "datastore":      fcd["datastore"],
            "capacity_gb":    fcd["capacity_gb"],
            "file_path":      fcd["file_path"] or "—",
            "category":       category,
            "reason":         reason,
            "claimed_by":     sorted({pv["cluster"] for pv in claiming}),
            "cns_known":      None if cns_id is None else cns_id is not False,
            "cns_cluster_id": cns_id or None,
            "cns_cluster":    cns_cluster,
            "attached":       attached,
            "vm_name":        (vm or {}).get("vm_name") or "—",
            "vm_power_state": (vm or {}).get("vm_power_state") or "—",
            "created":        fcd["created"].isoformat() if age is not None else None,
            "age_hours":      age,
            "too_young":      too_young,
            "looks_like_csi": bool(_CSI_NAME_RE.match(fcd["name"])),
            "actionable":     actionable,
        })
    disks.sort(key=lambda d: (not d["actionable"], -d["capacity_gb"]))
    return {
        "disks": disks,
        "counts": counts,
        "complete": not problems,
        "problems": problems,
        "cns": cns_state,
        "clusters": [{"name": c.name, "csi_cluster_id": c.csi_cluster_id} for c in clusters],
        "min_age_hours": UNCLAIMED_MIN_AGE_HOURS,
        "scanned_at": now.isoformat(),
    }


def get_container_volumes() -> list[dict]:
    """
    Return PVCs backed by the vSphere CSI driver in every configured cluster,
    sourced from Kubernetes. Fails closed, naming the cluster that could not be read.
    """
    import k8s_client

    def read(cluster):
        pvs = k8s_client.get_k8s_persistent_volumes(cluster)
        vsphere_pv_map = {pv["name"]: pv for pv in pvs if pv.get("driver") == VSPHERE_CSI_DRIVER}
        pvcs = k8s_client.get_k8s_persistent_volume_claims(cluster) if vsphere_pv_map else []
        return vsphere_pv_map, pvcs

    result = []
    for cluster in k8s_client.CLUSTERS:
        vsphere_pv_map, pvcs = k8s_client.read_cluster(cluster, read)
        for pvc in pvcs:
            pv_name = pvc.get("volume_name") or ""
            if pv_name not in vsphere_pv_map:
                continue
            pv = vsphere_pv_map[pv_name]
            result.append({
                "cluster": cluster.name,
                "name": pvc["name"],
                "namespace": pvc["namespace"],
                "pv_name": pv_name,
                "volume_handle": pv.get("volume_handle") or "—",
                "capacity": pvc.get("capacity") or pv.get("capacity") or "—",
                "status": pvc.get("status") or "—",
                "storage_class": pvc.get("storage_class") or "—",
                "access_modes": pvc.get("access_modes") or [],
                "created": pvc.get("created"),
            })
    return result
