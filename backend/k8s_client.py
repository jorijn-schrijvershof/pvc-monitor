import json
import logging
import os
from typing import NamedTuple

from kubernetes import client, config
from kubernetes.client import ApiClient, Configuration
from kubernetes.client.rest import ApiException

from settings import ConfigError, Unavailable, env_bool, env_str

logger = logging.getLogger(__name__)

KUBECONFIG_PATH = env_str("KUBECONFIG_PATH")
K8S_CONTEXT     = env_str("K8S_CONTEXT") or None
K8S_SERVER      = env_str("K8S_SERVER")
K8S_TOKEN       = env_str("K8S_TOKEN")
K8S_VERIFY_SSL  = env_bool("K8S_VERIFY_SSL", True)
K8S_CA_FILE     = env_str("K8S_CA_FILE")  # CA bundle for K8S_SERVER
VSPHERE_DRIVER  = "csi.vsphere.vmware.com"


class Cluster(NamedTuple):
    name: str
    source: str                       # "env", "in_cluster" or "kubeconfig"
    kubeconfig: str | None = None
    context: str | None = None
    csi_cluster_id: str | None = None  # cluster-id from this cluster's vSphere CSI configuration


_CLUSTER_KEYS = {"name", "in_cluster", "kubeconfig", "context", "csi_cluster_id"}


def _optional_str(entry: dict, key: str, where: str) -> str | None:
    value = entry.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"K8S_CLUSTERS: {where}: {key} must be a string")
    return value.strip() or None


def load_clusters() -> list[Cluster]:
    """The configured clusters; raises ConfigError with a clear message.

    K8S_CLUSTERS is a JSON list, for example
      [{"name": "local", "in_cluster": true, "csi_cluster_id": "cluster-a"},
       {"name": "b", "kubeconfig": "/etc/pvc-monitor/kubeconfigs/b", "context": "b", "csi_cluster_id": "cluster-b"}]
    Without it the single cluster from K8S_SERVER/K8S_TOKEN, KUBECONFIG_PATH or the
    in-cluster service account is used, named K8S_CLUSTER_NAME (default "default").
    """
    raw = env_str("K8S_CLUSTERS")
    if not raw:
        return [Cluster(
            name=env_str("K8S_CLUSTER_NAME") or "default",
            source="env",
            csi_cluster_id=env_str("K8S_CSI_CLUSTER_ID") or None,
        )]
    if KUBECONFIG_PATH or K8S_SERVER or K8S_TOKEN or env_str("K8S_CLUSTER_NAME") or env_str("K8S_CSI_CLUSTER_ID"):
        raise ConfigError(
            "K8S_CLUSTERS cannot be combined with KUBECONFIG_PATH, K8S_SERVER, K8S_TOKEN, "
            "K8S_CLUSTER_NAME or K8S_CSI_CLUSTER_ID; describe every cluster in K8S_CLUSTERS"
        )
    try:
        entries = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"K8S_CLUSTERS is not valid JSON: {exc.msg} at position {exc.pos}") from None
    if not isinstance(entries, list) or not entries:
        raise ConfigError("K8S_CLUSTERS must be a non-empty JSON list of objects")

    clusters: list[Cluster] = []
    for i, entry in enumerate(entries):
        where = f"entry {i + 1}"
        if not isinstance(entry, dict):
            raise ConfigError(f"K8S_CLUSTERS: {where} must be an object")
        unknown = set(entry) - _CLUSTER_KEYS
        if unknown:
            raise ConfigError(f"K8S_CLUSTERS: {where}: unknown key(s) {', '.join(sorted(unknown))}")
        name = _optional_str(entry, "name", where)
        if not name:
            raise ConfigError(f"K8S_CLUSTERS: {where}: name is required")
        where = f"cluster {name!r}"
        in_cluster = entry.get("in_cluster", False)
        if not isinstance(in_cluster, bool):
            raise ConfigError(f"K8S_CLUSTERS: {where}: in_cluster must be true or false")
        kubeconfig = _optional_str(entry, "kubeconfig", where)
        context = _optional_str(entry, "context", where)
        if in_cluster == bool(kubeconfig):
            raise ConfigError(f"K8S_CLUSTERS: {where}: set exactly one of in_cluster: true or kubeconfig")
        if in_cluster and context:
            raise ConfigError(f"K8S_CLUSTERS: {where}: context needs kubeconfig")
        if kubeconfig and not os.path.isfile(kubeconfig):
            raise ConfigError(f"K8S_CLUSTERS: {where}: kubeconfig file {kubeconfig} does not exist")
        clusters.append(Cluster(
            name=name,
            source="in_cluster" if in_cluster else "kubeconfig",
            kubeconfig=kubeconfig,
            context=context,
            csi_cluster_id=_optional_str(entry, "csi_cluster_id", where),
        ))

    names = [c.name for c in clusters]
    if len(set(names)) != len(names):
        raise ConfigError("K8S_CLUSTERS: cluster names must be unique")
    ids = [c.csi_cluster_id for c in clusters if c.csi_cluster_id]
    if len(set(ids)) != len(ids):
        raise ConfigError("K8S_CLUSTERS: csi_cluster_id values must be unique")
    return clusters


CLUSTERS = load_clusters()
SNAPSHOT_GROUP  = "snapshot.storage.k8s.io"
SNAPSHOT_VER    = "v1"

# (connect, read) timeout in seconds for every API call.
REQUEST_TIMEOUT = (10, 60)
# Objects per list page; only one page of full objects is held at a time.
PAGE_SIZE = 250

if K8S_SERVER and K8S_TOKEN and not K8S_VERIFY_SSL:
    logger.warning(
        "Kubernetes: K8S_VERIFY_SSL=false, the API server certificate is NOT verified "
        "and K8S_TOKEN can be intercepted"
    )


def _get_api_client(cluster: Cluster | None = None) -> ApiClient:
    """
    Build an ApiClient for one configured cluster (default: the first).

    A cluster from K8S_CLUSTERS uses its kubeconfig (and context) or the
    in-cluster service account. The single-cluster form ("env") uses, by priority:
      1. K8S_SERVER + K8S_TOKEN  — direct bearer token; most reliable for OIDC clusters
         where kubeconfig exec/OIDC plugins are unavailable server-side.
      2. KUBECONFIG_PATH         — load a specific kubeconfig file (supports OIDC via
         oauthlib if the id-token / refresh-token are present).
      3. In-cluster config       — service account mounted inside a pod.
      4. ~/.kube/config          — default kubeconfig as last resort.
    """
    cluster = cluster or CLUSTERS[0]
    if cluster.source != "env":
        try:
            if cluster.source == "in_cluster":
                cfg = Configuration()
                config.load_incluster_config(client_configuration=cfg)
                return ApiClient(configuration=cfg)
            return config.new_client_from_config(config_file=cluster.kubeconfig, context=cluster.context)
        except config.ConfigException as exc:
            logger.warning("Kubernetes: cluster %r has no usable configuration: %r", cluster.name, str(exc))
            raise Unavailable(f"Kubernetes cluster {cluster.name!r} is not configured") from exc

    # Option 1: explicit server + bearer token (best for OIDC in a backend context)
    if K8S_SERVER and K8S_TOKEN:
        cfg = Configuration()
        cfg.host                    = K8S_SERVER
        cfg.api_key["authorization"] = K8S_TOKEN
        cfg.api_key_prefix["authorization"] = "Bearer"
        cfg.verify_ssl              = K8S_VERIFY_SSL
        if K8S_CA_FILE:
            cfg.ssl_ca_cert = K8S_CA_FILE
        return ApiClient(configuration=cfg)

    # Options 2-4: kubeconfig-based (OIDC token refresh handled by oauthlib)
    try:
        if KUBECONFIG_PATH:
            return config.new_client_from_config(
                config_file=KUBECONFIG_PATH,
                context=K8S_CONTEXT,
            )
        try:
            cfg = Configuration()
            config.load_incluster_config(client_configuration=cfg)
            return ApiClient(configuration=cfg)
        except config.ConfigException:
            return config.new_client_from_config(context=K8S_CONTEXT)
    except config.ConfigException as exc:
        logger.warning("Kubernetes: no usable configuration: %r", str(exc))
        raise Unavailable("Kubernetes is not configured") from exc


def _list_typed(list_call, **kwargs):
    """Yield every item of a typed list call, one page at a time.

    Any error, including an expired continue token (410), propagates: callers
    must never work with a partial list.
    """
    token = None
    while True:
        page = list_call(limit=PAGE_SIZE, _continue=token, _request_timeout=REQUEST_TIMEOUT, **kwargs)
        yield from page.items or []
        token = page.metadata._continue if page.metadata else None
        if not token:
            return


def _list_custom(custom: client.CustomObjectsApi, plural: str):
    """Yield every item of a cluster-wide custom object list, one page at a time."""
    token = None
    while True:
        page = custom.list_cluster_custom_object(
            group=SNAPSHOT_GROUP, version=SNAPSHOT_VER, plural=plural,
            limit=PAGE_SIZE, _continue=token, _request_timeout=REQUEST_TIMEOUT,
        )
        yield from page.get("items") or []
        token = (page.get("metadata") or {}).get("continue")
        if not token:
            return


def check_k8s_connectivity() -> dict:
    """Connectivity of the first configured cluster."""
    try:
        with _get_api_client() as api:
            ver = client.VersionApi(api).get_code(_request_timeout=REQUEST_TIMEOUT)
        return {
            "connected": True,
            "server_version": f"v{ver.major}.{ver.minor}",
            "git_version": ver.git_version,
        }
    except Unavailable as exc:
        return {"connected": False, "configured": False, "error": str(exc)}
    except Exception:
        logger.exception("Kubernetes: connectivity check failed")
        return {"connected": False, "configured": True, "error": "connection failed, see the server log"}


def get_k8s_persistent_volumes(cluster: Cluster | None = None) -> list[dict]:
    """All PVs of one cluster. Raises if the list cannot be read completely:
    the result decides which vSphere disks count as claimed."""
    result = []
    with _get_api_client(cluster) as api:
        for pv in _list_typed(client.CoreV1Api(api).list_persistent_volume):
            if pv.spec.csi:
                driver       = pv.spec.csi.driver
                volume_handle = pv.spec.csi.volume_handle
                fs_type      = pv.spec.csi.fs_type or "ext4"
            elif pv.spec.vsphere_volume:
                driver       = "kubernetes.io/vsphere-volume"
                volume_handle = pv.spec.vsphere_volume.volume_path
                fs_type      = pv.spec.vsphere_volume.fs_type or "ext4"
            else:
                driver       = _pv_plugin_name(pv)
                volume_handle = None
                fs_type      = None

            result.append({
                "name": pv.metadata.name,
                "driver": driver,
                "volume_handle": volume_handle,
                "capacity": (pv.spec.capacity or {}).get("storage", "N/A"),
                "access_modes": pv.spec.access_modes or [],
                "reclaim_policy": pv.spec.persistent_volume_reclaim_policy,
                "status": pv.status.phase if pv.status else None,
                "storage_class": pv.spec.storage_class_name or "N/A",
                "claim_ref": (
                    f"{pv.spec.claim_ref.namespace}/{pv.spec.claim_ref.name}"
                    if pv.spec.claim_ref else None
                ),
                "fs_type": fs_type,
                "created": pv.metadata.creation_timestamp.isoformat() if pv.metadata.creation_timestamp else None,
            })
    return result


def read_cluster(cluster: Cluster, fn):
    """Return fn(cluster). Any failure becomes an Unavailable that names the
    cluster (fixed text for clients; the detail goes to the log)."""
    try:
        return fn(cluster)
    except Unavailable as exc:
        raise Unavailable(f"Kubernetes cluster {cluster.name!r}: {exc}. No result is produced.") from exc
    except Exception:
        logger.exception("Kubernetes: reading cluster %r failed", cluster.name)
        raise Unavailable(
            f"Kubernetes cluster {cluster.name!r} could not be read completely. "
            "No result is produced; see the server log."
        ) from None


def get_pvs_of_all_clusters() -> list[dict]:
    """Every PV of every configured cluster, each tagged with its cluster name.

    Fails closed: if the list of any cluster cannot be read completely, raise
    an Unavailable naming that cluster instead of returning the others.
    """
    result = []
    for cluster in CLUSTERS:
        pvs = read_cluster(cluster, get_k8s_persistent_volumes)
        result.extend({**pv, "cluster": cluster.name} for pv in pvs)
    return result


def _pv_plugin_name(pv) -> str:
    """Best-effort plugin name for non-CSI, non-vSphere PVs."""
    spec = pv.spec
    if spec.nfs:           return "nfs"
    if spec.host_path:     return "hostPath"
    if spec.iscsi:         return "iscsi"
    if spec.fc:            return "fc"
    if spec.rbd:           return "rbd"
    if spec.aws_elastic_block_store: return "awsEBS"
    if spec.azure_disk:    return "azureDisk"
    if spec.gce_persistent_disk:     return "gcePD"
    return "unknown"


def get_k8s_persistent_volume_claims(cluster: Cluster | None = None) -> list[dict]:
    result = []
    with _get_api_client(cluster) as api:
        v1 = client.CoreV1Api(api)
        for pvc in _list_typed(v1.list_persistent_volume_claim_for_all_namespaces):
            requested = (pvc.spec.resources.requests or {}).get("storage", "N/A") if pvc.spec.resources else "N/A"
            actual    = ((pvc.status.capacity if pvc.status else None) or {}).get("storage", requested)
            result.append({
                "name": pvc.metadata.name,
                "namespace": pvc.metadata.namespace,
                "status": pvc.status.phase if pvc.status else None,
                "volume_name": pvc.spec.volume_name or "—",
                "capacity": actual,
                "access_modes": pvc.spec.access_modes or [],
                "storage_class": pvc.spec.storage_class_name or "N/A",
                "created": pvc.metadata.creation_timestamp.isoformat() if pvc.metadata.creation_timestamp else None,
            })
    return result


def get_k8s_volume_snapshots() -> list[dict]:
    result = []
    with _get_api_client() as api:
        try:
            for snap in _list_custom(client.CustomObjectsApi(api), "volumesnapshots"):
                meta   = snap.get("metadata") or {}
                spec   = snap.get("spec") or {}
                status = snap.get("status") or {}
                result.append({
                    "name": meta.get("name"),
                    "namespace": meta.get("namespace"),
                    "source_pvc": (spec.get("source") or {}).get("persistentVolumeClaimName"),
                    "snapshot_class": spec.get("volumeSnapshotClassName"),
                    "content_name": status.get("boundVolumeSnapshotContentName"),
                    "ready": status.get("readyToUse", False),
                    "restore_size": status.get("restoreSize"),
                    "created": meta.get("creationTimestamp"),
                })
        except ApiException as exc:
            if exc.status in (404, 405):
                raise Unavailable("VolumeSnapshot CRD not installed") from exc
            raise
    return result


def get_k8s_volume_snapshot_contents() -> list[dict]:
    result = []
    with _get_api_client() as api:
        try:
            for content in _list_custom(client.CustomObjectsApi(api), "volumesnapshotcontents"):
                meta     = content.get("metadata") or {}
                spec     = content.get("spec") or {}
                status   = content.get("status") or {}
                snap_ref = spec.get("volumeSnapshotRef") or {}
                result.append({
                    "name": meta.get("name"),
                    "driver": spec.get("driver"),
                    "snapshot_handle": status.get("snapshotHandle") or (spec.get("source") or {}).get("snapshotHandle"),
                    "bound_snapshot": f"{snap_ref.get('namespace')}/{snap_ref.get('name')}" if snap_ref.get("name") else None,
                    "reclaim_policy": spec.get("deletionPolicy"),
                    "ready": status.get("readyToUse", False),
                    "restore_size": status.get("restoreSize"),
                    "created": meta.get("creationTimestamp"),
                })
        except ApiException as exc:
            if exc.status in (404, 405):
                raise Unavailable("VolumeSnapshotContent CRD not installed") from exc
            raise
    return result
