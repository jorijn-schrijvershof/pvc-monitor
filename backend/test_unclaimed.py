"""Tests for multi-cluster unclaimed-disk detection, using fakes only.

Nothing here talks to a real vCenter or cluster; the CNS query in particular is
only checked against pyVmomi's own type definitions.
"""
import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException
from pyVmomi import vim

import auth
import k8s_client
import main
import vcenter_client
from k8s_client import Cluster
from settings import ConfigError, Unavailable

CSI = "csi.vsphere.vmware.com"
NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=10)


# ── Fakes ────────────────────────────────────────────────────────────────────

def csi_name(fcd_id):
    """A name in the vSphere CSI pattern, derived from the id."""
    return f"pvc-{uuid.uuid5(uuid.NAMESPACE_URL, fcd_id)}"


def fcd(fcd_id, name=None, created=OLD, path=None, ds="ds1"):
    cfg = SimpleNamespace(
        id=SimpleNamespace(id=fcd_id), name=name or csi_name(fcd_id), capacityInMB=2048, createTime=created,
        backing=SimpleNamespace(filePath=path or f"[{ds}] fcd/{fcd_id}.vmdk"),
    )
    return SimpleNamespace(config=cfg)


class FakeDatastore(vim.Datastore):
    """A real vim.Datastore (pyVmomi type checks pass) whose name needs no vCenter."""
    name = property(lambda self: self._moId)


VALID = "valid-1"  # a CSI-claimed disk that CNS knows, so the CNS answer is validated


class FakeVCenter:
    """content/si fakes: datastores -> FCDs, VMs, associations, and failures to inject.

    associations: FCD id -> VM ids that vSphere associates with the disk; ids not
    listed have none. "fail" makes the call raise; an id mapped to "fault" gets an
    answer with a fault; an id mapped to "missing" is left out of the answer.
    validator adds the disk VALID to the first datastore (see the setup fixture).
    """

    def __init__(self, monkeypatch, datastores, vms=(), failing_ds=(), failing_fcds=(), vm_scan_fails=False,
                 associations=None, validator=True):
        self.destroyed, self.logouts, self.assoc_calls = [], [], []
        datastores = {name: list(disks) for name, disks in datastores.items()}
        if validator:
            next(iter(datastores.values())).append(fcd(VALID))
        self.ds = {name: FakeDatastore(name) for name in datastores}
        self.fcds = datastores
        associations = associations if associations is not None else {}
        self.failing_ds, self.failing_fcds = set(failing_ds), set(failing_fcds)

        def view_for(types):
            if types == [vim.Datastore]:
                items = list(self.ds.values())
            elif vm_scan_fails:
                items = BadList()
            else:
                items = list(vms)
            return SimpleNamespace(view=items, Destroy=lambda: self.destroyed.append(types[0]))

        def list_fcds(ds):
            if ds.name in self.failing_ds:
                raise vim.fault.NoPermission(privilegeId="Datastore.FileManagement")
            return [f.config.id.id for f in self.fcds[ds.name]]

        def retrieve(ref, ds):
            if ref in self.failing_fcds:
                raise RuntimeError("retrieve failed")
            return next(f for f in self.fcds[ds.name] if f.config.id.id == ref)

        def snapshots(ref, ds):
            return SimpleNamespace(snapshots=[SimpleNamespace(
                id=SimpleNamespace(id=f"snap-of-{ref}"), createTime=OLD, description="")])

        def associations_of(ids):
            assert all(isinstance(spec, vim.vslm.vcenter.RetrieveVStorageObjSpec) for spec in ids)
            self.assoc_calls.append([spec.id.id for spec in ids])
            if associations == "fail":
                raise vim.fault.NoPermission(privilegeId="System.View")
            answers = []
            for spec in ids:
                vms = associations.get(spec.id.id, [])
                if vms == "missing":
                    continue
                answers.append(SimpleNamespace(
                    id=SimpleNamespace(id=spec.id.id),
                    fault=vim.fault.NotFound() if vms == "fault" else None,
                    vmDiskAssociations=[] if vms == "fault" else [SimpleNamespace(vmId=v, diskKey=2000) for v in vms],
                ))
            return answers

        content = SimpleNamespace(
            rootFolder=object(),
            viewManager=SimpleNamespace(CreateContainerView=lambda root, types, rec: view_for(types)),
            vStorageObjectManager=SimpleNamespace(
                ListVStorageObject=list_fcds, RetrieveVStorageObject=retrieve,
                RetrieveSnapshotInfo=snapshots, RetrieveVStorageObjectAssociations=associations_of,
            ),
        )
        self.si = SimpleNamespace(RetrieveContent=lambda: content, _stub=SimpleNamespace(cookie="c"))
        monkeypatch.setattr(vcenter_client, "VCENTER_HOST", "vc.test")
        monkeypatch.setattr(vcenter_client, "SmartConnect", lambda **kw: self.si)
        monkeypatch.setattr(vcenter_client, "Disconnect", self.logouts.append)


class BadList:
    def __iter__(self):
        raise RuntimeError("no VM read privilege")


def attached_vm(fcd_id, name="node-1"):
    backing = SimpleNamespace(backingObjectId=fcd_id, fileName=f"[ds1] fcd/{fcd_id}.vmdk", datastore=None)
    disk = vim.vm.device.VirtualDisk(capacityInKB=2 * 1024 * 1024)
    disk.backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(fileName=backing.fileName, backingObjectId=fcd_id)
    return SimpleNamespace(
        name=name, runtime=SimpleNamespace(powerState="poweredOn"),
        config=SimpleNamespace(hardware=SimpleNamespace(device=[disk])),
    )


def pv(cluster, handle, name="pv", claim="ns/claim", driver=CSI):
    return {"name": name, "driver": driver, "volume_handle": handle, "claim_ref": claim,
            "cluster": cluster, "status": "Bound", "storage_class": "sc"}


CLUSTERS = [Cluster("a", "in_cluster", csi_cluster_id="id-a"),
            Cluster("b", "kubeconfig", "/k/b", csi_cluster_id="id-b")]


@pytest.fixture
def setup(monkeypatch):
    """Configure clusters, their PVs and the CNS answer; returns a scan runner."""
    def run(pvs=(), cns=None, clusters=CLUSTERS, cns_error=None, validated=True):
        if validated:  # the PV and CNS entry for FakeVCenter's VALID disk
            pvs = [*pvs, pv("a", VALID)]
            cns = {VALID: "id-a", **(cns or {})}
        monkeypatch.setattr(k8s_client, "CLUSTERS", list(clusters))
        monkeypatch.setattr(k8s_client, "get_pvs_of_all_clusters", lambda: list(pvs))

        def query(si):
            if cns_error:
                raise cns_error
            return dict(cns or {})

        monkeypatch.setattr(vcenter_client, "_query_cns_volumes", query)
        vcenter_client._scan_cache.clear()
        return vcenter_client.get_orphaned_fcds()

    return run


def by_id(result):
    return {d["fcd_id"]: d for d in result["disks"]}


# ── Configuration ────────────────────────────────────────────────────────────

class TestClusterConfig:
    def load(self, monkeypatch, value=None, **env):
        for name in ("K8S_CLUSTERS", "K8S_CLUSTER_NAME", "K8S_CSI_CLUSTER_ID"):
            monkeypatch.delenv(name, raising=False)
        if value is not None:
            monkeypatch.setenv("K8S_CLUSTERS", value if isinstance(value, str) else json.dumps(value))
        for name, v in env.items():
            monkeypatch.setenv(name, v)
        return k8s_client.load_clusters()

    def test_single_cluster_default(self, monkeypatch):
        assert self.load(monkeypatch) == [Cluster("default", "env")]

    def test_single_cluster_with_name_and_id(self, monkeypatch):
        clusters = self.load(monkeypatch, K8S_CLUSTER_NAME="prod", K8S_CSI_CLUSTER_ID="cid")
        assert clusters == [Cluster("prod", "env", csi_cluster_id="cid")]

    def test_list(self, monkeypatch, tmp_path):
        kubeconfig = tmp_path / "b"
        kubeconfig.write_text("apiVersion: v1")
        clusters = self.load(monkeypatch, [
            {"name": "a", "in_cluster": True, "csi_cluster_id": "id-a"},
            {"name": "b", "kubeconfig": str(kubeconfig), "context": "ctx"},
        ])
        assert clusters == [Cluster("a", "in_cluster", csi_cluster_id="id-a"),
                            Cluster("b", "kubeconfig", str(kubeconfig), "ctx")]

    @pytest.mark.parametrize("value, message", [
        ("not json", "not valid JSON"),
        ([], "non-empty JSON list"),
        (["a"], "must be an object"),
        ([{"in_cluster": True}], "name is required"),
        ([{"name": "a"}], "exactly one of"),
        ([{"name": "a", "in_cluster": True, "kubeconfig": "/x"}], "exactly one of"),
        ([{"name": "a", "kubeconfig": "/does/not/exist"}], "does not exist"),
        ([{"name": "a", "in_cluster": True, "context": "c"}], "context needs kubeconfig"),
        ([{"name": "a", "in_cluster": "yes"}], "in_cluster must be true or false"),
        ([{"name": "a", "in_cluster": True, "token": "x"}], "unknown key"),
        ([{"name": "a", "in_cluster": True}, {"name": "a", "in_cluster": True}], "names must be unique"),
        ([{"name": "a", "in_cluster": True, "csi_cluster_id": "x"},
          {"name": "b", "in_cluster": True, "csi_cluster_id": "x"}], "csi_cluster_id values must be unique"),
    ])
    def test_invalid(self, monkeypatch, value, message):
        with pytest.raises(ConfigError, match=message):
            self.load(monkeypatch, value)

    def test_kubeconfig_entry_reaches_its_own_server(self, monkeypatch, tmp_path):
        kubeconfig = tmp_path / "kc"
        kubeconfig.write_text(json.dumps({
            "apiVersion": "v1", "kind": "Config", "current-context": "one",
            "clusters": [{"name": c, "cluster": {"server": f"https://{c}.test:6443"}} for c in ("one", "two")],
            "users": [{"name": "u", "user": {"token": "t"}}],
            "contexts": [{"name": c, "context": {"cluster": c, "user": "u"}} for c in ("one", "two")],
        }))
        clusters = self.load(monkeypatch, [{"name": "b", "kubeconfig": str(kubeconfig), "context": "two"}])
        assert k8s_client._get_api_client(clusters[0]).configuration.host == "https://two.test:6443"

    def test_list_cannot_be_mixed_with_single_cluster_settings(self, monkeypatch):
        with pytest.raises(ConfigError, match="cannot be combined"):
            self.load(monkeypatch, [{"name": "a", "in_cluster": True}], K8S_CLUSTER_NAME="x")


# ── Classification matrix ────────────────────────────────────────────────────

@pytest.mark.parametrize("claims, cns, expected", [
    # claimed by a configured cluster
    ([("a", True)], "id-a", "in_use"),
    ([("a", True)], None, "in_use"),                 # CNS unknown never demotes a claim to a report
    ([("a", True)], "", "in_use"),                   # CNS has no cluster-id to compare
    ([("a", True)], False, "inconsistent"),          # claimed, CNS does not know it
    ([("a", True)], "id-b", "inconsistent"),         # CNS names another configured cluster
    ([("a", True)], "id-x", "inconsistent"),         # CNS names a cluster that is not the holder
    ([("a", False)], False, "in_use"),               # in-tree claim: CNS rule not applied
    ([("a", True), ("b", True)], "id-a", "inconsistent"),  # two PVs claim one disk
    # not claimed
    ([], "id-b", "orphan_known_to_cns"),
    ([], "id-x", "foreign"),
    ([], False, "candidate"),
    ([], None, "unknown"),
    ([], "", "unknown"),
])
def test_classification(claims, cns, expected):
    assert vcenter_client.classify(claims, cns, CLUSTERS)[0] == expected


def test_holder_without_configured_id_is_not_compared():
    clusters = [Cluster("a", "env"), Cluster("b", "kubeconfig", "/k", csi_cluster_id="id-b")]
    assert vcenter_client.classify([("a", True)], "id-x", clusters)[0] == "in_use"


def test_unplaceable_cns_id_is_unknown_not_foreign():
    """With a cluster that has no csi_cluster_id, an unknown id might be that cluster's."""
    clusters = [Cluster("a", "env"), Cluster("b", "kubeconfig", "/k", csi_cluster_id="id-b")]
    assert vcenter_client.classify([], "id-x", clusters)[0] == "unknown"
    assert vcenter_client.classify([], "id-b", clusters)[0] == "orphan_known_to_cns"


# ── Scan end to end with a fake vCenter ──────────────────────────────────────

def test_union_over_clusters_and_categories(monkeypatch, setup):
    vc = FakeVCenter(monkeypatch, {"ds1": [
        fcd("in-a"), fcd("in-b"), fcd("cand-1"), fcd("known-a"), fcd("foreign-1"), fcd("drift-1"),
    ]})
    result = setup(
        pvs=[pv("a", "IN-A"), pv("b", "in-b"), pv("b", "drift-1")],
        cns={"in-a": "id-a", "in-b": "id-b", "known-a": "id-a", "foreign-1": "id-other", "drift-1": "id-a"},
    )
    disks = by_id(result)
    assert set(disks) == {"cand-1", "known-a", "foreign-1", "drift-1"}  # in_use disks are not listed
    assert disks["cand-1"]["category"] == "candidate" and disks["cand-1"]["actionable"]
    assert disks["known-a"]["category"] == "orphan_known_to_cns" and disks["known-a"]["cns_cluster"] == "a"
    assert disks["foreign-1"]["category"] == "foreign" and not disks["foreign-1"]["actionable"]
    assert disks["drift-1"]["category"] == "inconsistent" and disks["drift-1"]["claimed_by"] == ["b"]
    assert result["counts"] == {"in_use": 3, "inconsistent": 1, "orphan_known_to_cns": 1, "foreign": 1,
                                "candidate": 1, "unknown": 0, "actionable": 2}
    assert result["complete"] is True and result["problems"] == []
    assert result["clusters"] == [{"name": "a", "csi_cluster_id": "id-a"}, {"name": "b", "csi_cluster_id": "id-b"}]
    assert result["cns"] == "ok" and result["scanned_at"]
    assert len(vc.logouts) == 1


def test_fails_closed_when_any_cluster_cannot_be_read(monkeypatch):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x")]})
    monkeypatch.setattr(k8s_client, "CLUSTERS", CLUSTERS)

    def list_pvs(cluster=None):
        if cluster.name == "b":
            raise ApiException(status=410, reason="Expired")
        return []

    monkeypatch.setattr(k8s_client, "get_k8s_persistent_volumes", list_pvs)
    with pytest.raises(Unavailable, match="cluster 'b'") as info:
        vcenter_client.get_orphaned_fcds()
    assert "Expired" not in str(info.value)


def test_unconfigured_cluster_named_in_error(monkeypatch):
    monkeypatch.setattr(k8s_client, "CLUSTERS", [Cluster("x", "in_cluster")])
    with pytest.raises(Unavailable, match="Kubernetes cluster 'x': Kubernetes cluster 'x' is not configured"):
        k8s_client.get_pvs_of_all_clusters()


def test_uuid_only_matching_regression(monkeypatch, setup):
    """A PVC or FCD named like another volume must not change attribution."""
    FakeVCenter(monkeypatch, {"ds1": [
        fcd("uuid-owner", name="static-disk"),   # statically provisioned, display name is not a PV name
        fcd("uuid-tenant", name="pv-owner"),      # an FCD carrying another PV's name
        fcd("uuid-loose", name="pv-tenant"),
    ]}, validator=False)
    pvs = [pv("a", "uuid-owner", name="pv-owner", claim="ns1/owner"),
           pv("a", "uuid-tenant", name="pv-tenant", claim="ns2/static-disk")]  # PVC named like the FCD
    result = setup(pvs=pvs, cns={"uuid-owner": "id-a", "uuid-tenant": "id-a", "uuid-loose": "id-a"}, validated=False)
    assert set(by_id(result)) == {"uuid-loose"}  # named like pv-tenant, but its UUID is not claimed

    vcenter_client._scan_cache.clear()
    volumes = vcenter_client.get_persistent_volumes()["volumes"]
    assert {(v["volume_id"], v["pv_name"]) for v in volumes} == {("uuid-owner", "pv-owner"),
                                                                 ("uuid-tenant", "pv-tenant")}

    vcenter_client._scan_cache.clear()
    snaps = vcenter_client.get_snapshots()["snapshots"]
    assert {(s["snapshot_id"], s["pv_name"], s["pvc_name"]) for s in snaps} == {
        ("snap-of-uuid-owner", "pv-owner", "owner"),
        ("snap-of-uuid-tenant", "pv-tenant", "static-disk"),
    }


def test_in_tree_volume_path_counts_as_claim(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("legacy", path="[ds1] kubevols/legacy.vmdk"), fcd("other")]})
    result = setup(pvs=[pv("a", "[DS1] kubevols/legacy.vmdk", driver="kubernetes.io/vsphere-volume")], cns={})
    assert set(by_id(result)) == {"other"}
    assert result["counts"]["in_use"] == 2
    assert result["complete"] is True


def test_unmatched_in_tree_pv_makes_unclaimed_disks_unknown(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("legacy", path="[ds1] kubevols/legacy.vmdk"), fcd("x"), fcd("y")]})
    in_tree = "kubernetes.io/vsphere-volume"
    result = setup(pvs=[pv("a", "[ds1] kubevols/legacy.vmdk", driver=in_tree),
                        pv("b", "[ds9] kubevols/gone-1.vmdk", driver=in_tree),
                        pv("b", "[ds9] kubevols/gone-2.vmdk", driver=in_tree)], cns={"y": "id-a"})
    disks = by_id(result)
    assert set(disks) == {"x", "y"}
    assert all(d["category"] == "unknown" and not d["actionable"] for d in disks.values())
    assert result["counts"]["in_use"] == 2 and result["counts"]["actionable"] == 0
    assert result["complete"] is False
    assert result["problems"] == [vcenter_client._IN_TREE_PROBLEM.format(cluster="b", count=2)]


def test_incomplete_datastore_and_disk_scan(monkeypatch, setup, caplog):
    FakeVCenter(monkeypatch, {"ds1": [fcd("ok-1"), fcd("broken-1")], "ds2": [fcd("hidden")]},
                failing_ds={"ds2"}, failing_fcds={"broken-1"})
    result = setup(cns={})
    assert set(by_id(result)) == {"ok-1"}
    assert result["complete"] is False
    assert sorted(result["problems"]) == ["Datastore ds1: disk broken-1 could not be retrieved",
                                          "Datastore ds2: First Class Disks could not be listed"]
    assert "Datastore.FileManagement" in caplog.text  # the privilege hint for NoPermission


def test_no_listable_datastore_is_an_error(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x")]}, failing_ds={"ds1"})
    with pytest.raises(Unavailable, match="No datastore could be listed"):
        setup(cns={})


def test_datastore_allow_list(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("in-scope")], "ds2": [fcd("out-of-scope")]})
    monkeypatch.setattr(vcenter_client, "VCENTER_DATASTORES", ["ds1", "ds9"])
    result = setup(cns={})
    assert set(by_id(result)) == {"in-scope"}
    assert result["problems"] == ["Datastore ds9 from VCENTER_DATASTORES was not found"]


def test_failed_vm_scan_makes_attachment_unknown(monkeypatch, setup):
    vc = FakeVCenter(monkeypatch, {"ds1": [fcd("x")]}, vm_scan_fails=True)
    result = setup(cns={})
    disk = by_id(result)["x"]
    assert disk["attached"] == "unknown" and disk["actionable"] is False
    assert disk["category"] == "candidate"
    assert result["complete"] is False and vcenter_client._VM_SCAN_PROBLEM in result["problems"]
    assert vim.VirtualMachine in vc.destroyed  # the view is still destroyed


def test_vm_without_readable_config_makes_attachment_unknown(monkeypatch, setup):
    unreadable = SimpleNamespace(name="hidden", config=None)
    FakeVCenter(monkeypatch, {"ds1": [fcd("x"), fcd("y")]}, vms=[unreadable, attached_vm("y")])
    result = setup(cns={})
    disks = by_id(result)
    assert disks["x"]["attached"] == "unknown" and not disks["x"]["actionable"]
    assert disks["y"]["attached"] == "yes"  # still known from the VMs that could be read
    assert result["complete"] is False and vcenter_client._VM_SCAN_PROBLEM in result["problems"]


def test_association_finds_a_vm_the_scan_cannot_see(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x"), fcd("y")]}, associations={"x": ["vm-42"]})
    result = setup(cns={})
    disks = by_id(result)
    assert disks["x"]["attached"] == "yes" and disks["x"]["vm_name"] == "vm-42" and not disks["x"]["actionable"]
    assert disks["y"]["attached"] == "no" and disks["y"]["actionable"]
    assert result["complete"] is True


@pytest.mark.parametrize("associations, unknown", [
    ("fail", {"x", "y"}),
    ({"x": "fault"}, {"x"}),
    ({"x": "missing"}, {"x"}),
])
def test_unanswered_association_makes_attachment_unknown(monkeypatch, setup, associations, unknown):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x"), fcd("y")]}, associations=associations)
    result = setup(cns={})
    disks = by_id(result)
    assert {d for d in disks if disks[d]["attached"] == "unknown"} == unknown
    assert not any(disks[d]["actionable"] for d in unknown)
    assert result["complete"] is False
    assert vcenter_client._ASSOC_PROBLEM.format(count=len(unknown)) in result["problems"]


def test_associations_asked_in_batches_for_listed_disks_only(monkeypatch, setup):
    monkeypatch.setattr(vcenter_client, "ASSOC_BATCH_SIZE", 2)
    vc = FakeVCenter(monkeypatch, {"ds1": [fcd(f"d{i}") for i in range(5)] + [fcd("claimed")]})
    setup(pvs=[pv("a", "claimed")], cns={"claimed": "id-a"})
    assert [len(call) for call in vc.assoc_calls] == [2, 2, 1]
    assert sorted(i for call in vc.assoc_calls for i in call) == [f"d{i}" for i in range(5)]


def test_attached_disk_is_not_actionable(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x")]}, vms=[attached_vm("x")])
    disk = by_id(setup(cns={}))["x"]
    assert disk["attached"] == "yes" and disk["vm_name"] == "node-1" and disk["actionable"] is False


def test_minimum_age(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [
        fcd("young", created=NOW - timedelta(hours=2)),
        fcd("old", created=NOW - timedelta(hours=30)),
        fcd("undated", created=None),
    ]})
    disks = by_id(setup(cns={}))
    assert disks["young"]["too_young"] is True and not disks["young"]["actionable"]
    assert disks["old"]["too_young"] is False and disks["old"]["actionable"]
    assert disks["undated"]["age_hours"] is None and not disks["undated"]["actionable"]

    monkeypatch.setattr(vcenter_client, "UNCLAIMED_MIN_AGE_HOURS", 1)
    assert by_id(setup(cns={}))["young"]["actionable"]


def test_candidate_needs_a_csi_name_to_be_actionable(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x", name="pvc-0a1b2c3d-1111-2222-3333-444455556666"),
                                      fcd("y", name="backup-disk"), fcd("z", name="other-disk")]})
    result = setup(cns={"z": "id-b"})
    disks = by_id(result)
    assert disks["x"]["looks_like_csi"] is True and disks["x"]["actionable"]
    assert disks["y"]["category"] == "candidate" and disks["y"]["looks_like_csi"] is False
    assert not disks["y"]["actionable"] and "not actionable" in disks["y"]["reason"]
    # The CSI name rule applies to candidates only.
    assert disks["z"]["category"] == "orphan_known_to_cns" and disks["z"]["actionable"]
    assert result["counts"]["actionable"] == 2


def test_cns_failure_keeps_the_list_and_marks_unknown(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x"), fcd("claimed")]})
    result = setup(pvs=[pv("a", "claimed")], cns_error=RuntimeError("vsanHealth unavailable"))
    disk = by_id(result)["x"]
    assert disk["category"] == "unknown" and disk["cns_known"] is None and not disk["actionable"]
    assert result["cns"] == "failed" and result["complete"] is False
    assert vcenter_client._CNS_PROBLEM in result["problems"]
    assert result["counts"]["in_use"] == 2


@pytest.mark.parametrize("pvs, cns, problem", [
    # CNS knows none of the claimed disks (for example a missing privilege)
    ([pv("a", "claimed")], {"elsewhere": "id-a"}, vcenter_client._CNS_EMPTY_PROBLEM),
    # CNS returned nothing at all
    ([pv("a", "claimed")], {}, vcenter_client._CNS_NO_VOLUMES_PROBLEM),
    # nothing claimed on the scanned datastores to check the answer against
    ([], {"elsewhere": "id-a"}, vcenter_client._CNS_UNCHECKED_PROBLEM),
    ([pv("a", "claimed", driver="kubernetes.io/vsphere-volume")], {"elsewhere": "id-a"},
     vcenter_client._CNS_UNCHECKED_PROBLEM),
])
def test_unvalidated_cns_answer_is_unknown(monkeypatch, setup, pvs, cns, problem):
    """Only a CNS answer that knows a claimed disk may make a disk a candidate."""
    FakeVCenter(monkeypatch, {"ds1": [fcd("x"), fcd("claimed")]}, validator=False)
    result = setup(pvs=pvs, cns=cns, validated=False)
    assert by_id(result)["x"]["category"] == "unknown" and result["counts"]["actionable"] == 0
    assert result["cns"] == "failed" and result["complete"] is False
    assert problem in result["problems"]


def test_cns_disabled_leaves_categories_unknown(monkeypatch, setup):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x")]})
    monkeypatch.setattr(vcenter_client, "VCENTER_CNS_ENABLED", False)
    result = setup(cns_error=AssertionError("must not be called"))
    assert result["cns"] == "disabled" and by_id(result)["x"]["category"] == "unknown"
    assert result["complete"] is True


# ── Scan lock ────────────────────────────────────────────────────────────────

def test_waiting_for_a_running_scan_is_bounded(monkeypatch):
    monkeypatch.setattr(vcenter_client, "VCENTER_SCAN_WAIT_SECONDS", 0.1)
    vcenter_client._scan_lock.acquire()
    try:
        with pytest.raises(Unavailable, match="scan is in progress"):
            vcenter_client.get_orphaned_fcds()
    finally:
        vcenter_client._scan_lock.release()


# ── The CNS query against pyVmomi's own types ────────────────────────────────

def test_cns_query_pages_through_results(monkeypatch):
    """Runs the real pyVmomi managed-object binding; only the SOAP transport is fake."""
    def volume(vid, cluster_id):
        return vim.cns.Volume(
            volumeId=vim.cns.VolumeId(id=vid),
            metadata=vim.cns.VolumeMetadata(containerCluster=vim.cns.ContainerCluster(
                clusterType="KUBERNETES", clusterId=cluster_id, vSphereUser="u")),
        )

    pages = {
        0: vim.cns.QueryResult(volumes=[volume("A-1", "id-a"), volume("b-1", "id-b")],
                               cursor=vim.cns.Cursor(offset=2, limit=2, totalRecords=3)),
        2: vim.cns.QueryResult(volumes=[volume("c-1", "")], cursor=vim.cns.Cursor(offset=3, limit=2, totalRecords=3)),
    }
    stubs = []

    class FakeStub:
        def __init__(self, **kwargs):
            self.kwargs, self.calls, self.cookie, self.dropped = kwargs, [], None, False
            stubs.append(self)

        def InvokeMethod(self, mo, info, args):
            self.calls.append((mo._moId, info.wsdlName, args[0]))
            return pages[args[0].cursor.offset]

        def DropConnections(self):
            self.dropped = True

    monkeypatch.setattr(vcenter_client, "SoapStubAdapter", FakeStub)
    monkeypatch.setattr(vcenter_client, "VCENTER_HOST", "vc.test")
    si = SimpleNamespace(_stub=SimpleNamespace(cookie='vmware_soap_session="abc"'))
    assert vcenter_client._query_cns_volumes(si) == {"a-1": "id-a", "b-1": "id-b", "c-1": ""}
    stub = stubs[0]
    assert stub.kwargs["path"] == "/vsanHealth" and stub.kwargs["version"] == "vsan.version.version11"
    assert stub.cookie == 'vmware_soap_session="abc"' and stub.dropped
    assert [(c[0], c[1]) for c in stub.calls] == [("cns-volume-manager", "CnsQueryVolume")] * 2
    assert stub.calls[1][2].cursor.offset == 2


def test_cns_query_stops_when_cursor_does_not_advance(monkeypatch):
    class FakeStub:
        def __init__(self, **kwargs):
            self.cookie = None

        def InvokeMethod(self, mo, info, args):
            return vim.cns.QueryResult(volumes=[vim.cns.Volume(volumeId=vim.cns.VolumeId(id="x"))],
                                       cursor=vim.cns.Cursor(offset=0, limit=1))

        def DropConnections(self):
            pass

    monkeypatch.setattr(vcenter_client, "SoapStubAdapter", FakeStub)
    with pytest.raises(RuntimeError, match="did not advance"):
        vcenter_client._query_cns_volumes(SimpleNamespace(_stub=SimpleNamespace(cookie="")))


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)
    return TestClient(main.app, base_url="https://testserver")


def test_api_returns_scan_object(monkeypatch, setup, api):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x")]}, failing_ds=())
    setup(cns={})
    body = api.get("/api/orphaned-fcds").json()
    assert set(body) >= {"disks", "counts", "complete", "problems", "clusters", "scanned_at", "cns"}


def test_api_names_the_failing_cluster(monkeypatch, api):
    def fail():
        raise Unavailable("Kubernetes cluster 'b' could not be read completely. No result is produced; see the server log.")

    monkeypatch.setattr(vcenter_client, "get_orphaned_fcds", fail)
    response = api.get("/api/orphaned-fcds")
    assert response.status_code == 503 and "'b'" in response.json()["detail"]


@pytest.mark.parametrize("path", ["/api/orphaned-fcds", "/api/snapshots", "/api/health"])
def test_api_answers_503_while_a_scan_holds_the_lock(monkeypatch, api, path):
    monkeypatch.setattr(vcenter_client, "VCENTER_SCAN_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(k8s_client, "get_pvs_of_all_clusters", lambda: [])
    vcenter_client._scan_lock.acquire()
    try:
        response = api.get(path)
    finally:
        vcenter_client._scan_lock.release()
    assert response.status_code == 503 and "scan is in progress" in response.json()["detail"]


def test_api_snapshots_returns_scan_object(monkeypatch, setup, api):
    FakeVCenter(monkeypatch, {"ds1": [fcd("x")]}, failing_fcds={"x"})
    monkeypatch.setattr(k8s_client, "get_pvs_of_all_clusters", lambda: [pv("a", VALID)])
    body = api.get("/api/snapshots").json()
    assert body["complete"] is False and body["problems"] == ["Datastore ds1: disk x could not be retrieved"]
    assert [s["pv_name"] for s in body["snapshots"]] == ["pv"]


def test_overview_reports_vsphere_failure_instead_of_idle(monkeypatch, api):
    monkeypatch.setattr(vcenter_client, "get_container_volumes", lambda: [
        {"cluster": "a", "name": "c", "namespace": "n", "pv_name": "pv", "volume_handle": "h", "status": "Bound"},
    ])

    def fail():
        raise RuntimeError("socket to secret-host")

    monkeypatch.setattr(vcenter_client, "get_persistent_volumes", fail)
    body = api.get("/api/overview").json()
    assert body["vsphere_error"] and "secret-host" not in body["vsphere_error"]
    assert body["volumes"][0]["usage_state"] == "unknown"


def test_overview_joins_by_cluster_and_handle(monkeypatch, api):
    monkeypatch.setattr(vcenter_client, "get_container_volumes", lambda: [
        {"cluster": "a", "name": "c1", "namespace": "n", "pv_name": "same", "volume_handle": "H1", "status": "Bound"},
        {"cluster": "b", "name": "c2", "namespace": "n", "pv_name": "same", "volume_handle": "h2", "status": "Bound"},
        {"cluster": "b", "name": "c3", "namespace": "n", "pv_name": "p3", "volume_handle": "h3", "status": "Bound"},
    ])
    monkeypatch.setattr(vcenter_client, "get_persistent_volumes", lambda: {"complete": True, "problems": [], "volumes": [
        {"cluster": "a", "volume_id": "h1", "pv_name": "same", "attached": "yes", "vm_name": "n1"},
        {"cluster": "b", "volume_id": "h2", "pv_name": "same", "attached": "no", "vm_name": "—"},
    ]})
    states = {v["pvc_name"]: v["usage_state"] for v in api.get("/api/overview").json()["volumes"]}
    assert states == {"c1": "active", "c2": "idle", "c3": "unknown"}  # c3: disk not found


def test_association_query_through_pyvmomi_binding():
    """Runs the real pyVmomi managed-object binding; only the SOAP transport is fake."""
    def answer(fcd_id, vm_ids=(), fault=None):
        return vim.vslm.vcenter.VStorageObjectAssociations(
            id=vim.vslm.ID(id=fcd_id), fault=fault,
            vmDiskAssociations=[vim.vslm.vcenter.VStorageObjectAssociations.VmDiskAssociations(vmId=v, diskKey=2000)
                                for v in vm_ids],
        )

    calls = []

    class FakeStub:
        def InvokeMethod(self, mo, info, args):
            calls.append((mo._moId, info.wsdlName, args[0]))
            return [answer("A-1", ["vm-1"]), answer("b-1"), answer("c-1", fault=vim.fault.NotFound())]

    vsom = vim.vslm.vcenter.VStorageObjectManager("VStorageObjectManager", FakeStub())
    ds = FakeDatastore("ds1")
    fcds = [{"id": i.lower(), "fcd_id": i, "ds": ds} for i in ("A-1", "b-1", "c-1", "d-1")]
    assert vcenter_client._query_associations(vsom, fcds) == {"a-1": ["vm-1"], "b-1": [], "c-1": None, "d-1": None}
    (mo_id, method, specs), = calls
    assert method == "RetrieveVStorageObjectAssociations"
    assert [(s.id.id, s.datastore) for s in specs] == [(f["fcd_id"], ds) for f in fcds]
