from __future__ import annotations

from tally import definition, screens
from tally.menu import BACK, STAY, Exit, Item
from tally.model import Cluster, CpuVendor, InstallTarget, Node, NodeRole, ProfileKey, Vswitch
from tally.observe import NodeState, Snapshot
from tally.paths import Paths
from tally.screens import ScreenState
from tally.stages.base import Ctx


def _node(name="cp1", role=NodeRole.CONTROLPLANE, ip="10.0.0.1", gateway="10.0.0.254"):
    return Node(name=name, role=role, cpu=CpuVendor.AMD, ip=ip, gateway=gateway)


def _snap(states=None, *, k8s=False, cilium=False):
    return Snapshot(
        states=states or {},
        orphans=[],
        k8s_reachable=k8s,
        cilium_installed=cilium,
    )


def _state(tmp_path, cluster, snap=None):
    paths = Paths(tmp_path / "talos", tmp_path / "talos-secrets")
    paths.ensure()
    ctx = Ctx(cluster=cluster, paths=paths)
    return ScreenState(ctx=ctx, snap=snap or _snap())


def _find(menu, label):
    for it in menu.items():
        if it.label == label:
            return it
    raise AssertionError(f"no item labelled {label!r}; have {[i.label for i in menu.items()]}")


def _find_prefix(menu, prefix):
    for it in menu.items():
        if it.label.startswith(prefix):
            return it
    raise AssertionError(f"no item starting {prefix!r}; have {[i.label for i in menu.items()]}")


# tally up -------------------------------------------------------------------


def test_tally_up_disabled_when_unset_name(tmp_path):
    cluster = Cluster(nodes=[_node()])  # no name -> a problem
    state = _state(tmp_path, cluster)
    up = _find(screens.main_menu(state), "Tally Up!")
    assert up.disabled is True
    assert up.hint  # first problem surfaced as hint


def test_tally_up_disabled_when_node_missing_ip(tmp_path):
    cluster = Cluster(nodes=[_node(ip="")], name="c")
    state = _state(tmp_path, cluster)
    up = _find(screens.main_menu(state), "Tally Up!")
    assert up.disabled is True


def test_tally_up_enabled_and_exits_when_clean(tmp_path):
    cluster = Cluster(nodes=[_node()], name="c")
    state = _state(tmp_path, cluster)
    up = _find(screens.main_menu(state), "Tally Up!")
    assert up.disabled is False
    assert up.run() == Exit("up")


def test_quit_returns_back(tmp_path):
    cluster = Cluster(nodes=[_node()], name="c")
    state = _state(tmp_path, cluster)
    assert _find(screens.main_menu(state), "Quit").run() is BACK


# locks ----------------------------------------------------------------------


def test_locks_honored_for_joined_node(tmp_path):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.JOINED}))
    menu = screens.node_menu(state, node)

    for field in ("name", "role", "cpu", "ip", "gateway"):
        it = _find_prefix(menu, f"{field}:")
        assert it.disabled is True, field
        assert it.hint == "locked: node is live", field

    for field in ("profile", "install"):
        it = _find_prefix(menu, f"{field}:")
        assert it.disabled is False, field
        assert it.hint is None, field


def test_vlan_ip_always_disabled(tmp_path):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.ABSENT}))
    it = _find_prefix(screens.node_menu(state, node), "vlan_ip:")
    assert it.disabled is True
    assert it.hint == "auto-assigned"


# field editing --------------------------------------------------------------


def test_edit_profile_updates_and_saves(tmp_path, monkeypatch):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.ABSENT}))
    monkeypatch.setattr(screens.prompts, "ask_profile", lambda default: ProfileKey.DB)

    it = _find_prefix(screens.node_menu(state, node), "profile:")
    assert it.run() is STAY
    assert node.profile is ProfileKey.DB

    reloaded = definition.load(state.ctx.paths.defn)
    assert reloaded.nodes[0].profile is ProfileKey.DB


def test_edit_ip_updates(tmp_path, monkeypatch):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.ABSENT}))
    monkeypatch.setattr(screens.prompts, "ask_text", lambda *a, **k: "10.0.0.9")

    it = _find_prefix(screens.node_menu(state, node), "ip:")
    it.run()
    assert node.ip == "10.0.0.9"
    assert definition.load(state.ctx.paths.defn).nodes[0].ip == "10.0.0.9"


def test_edit_cancel_leaves_unchanged(tmp_path, monkeypatch):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.ABSENT}))
    monkeypatch.setattr(screens.prompts, "ask_profile", lambda default: None)

    it = _find_prefix(screens.node_menu(state, node), "profile:")
    assert it.run() is STAY
    assert node.profile is ProfileKey.GENERIC


def test_edit_firmware_empty_clears_to_none(tmp_path, monkeypatch):
    node = _node()
    node.nic_firmware_ext = "ref"
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.ABSENT}))
    monkeypatch.setattr(screens.prompts, "ask_text", lambda *a, **k: "")

    _find_prefix(screens.node_menu(state, node), "nic_firmware_ext:").run()
    assert node.nic_firmware_ext is None


# delete ---------------------------------------------------------------------


def test_delete_node_confirmed(tmp_path, monkeypatch):
    node = _node()
    keep = _node("worker1", role=NodeRole.WORKER)
    cluster = Cluster(nodes=[node, keep], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.ABSENT, "worker1": NodeState.ABSENT}))
    monkeypatch.setattr(screens, "confirm", lambda *a, **k: True)

    it = _find(screens.node_menu(state, node), "Delete node")
    assert it.run() is BACK
    assert node not in cluster.nodes
    assert [n.name for n in definition.load(state.ctx.paths.defn).nodes] == ["worker1"]


def test_delete_node_declined(tmp_path, monkeypatch):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.ABSENT}))
    monkeypatch.setattr(screens, "confirm", lambda *a, **k: False)

    it = _find(screens.node_menu(state, node), "Delete node")
    assert it.run() is STAY
    assert node in cluster.nodes


def test_delete_live_node_warns(tmp_path, monkeypatch):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster, _snap({"cp1": NodeState.JOINED}))
    seen = {}

    def fake_confirm(label, **k):
        seen["label"] = label
        return True

    monkeypatch.setattr(screens, "confirm", fake_confirm)
    _find(screens.node_menu(state, node), "Delete node").run()
    assert "live" in seen["label"]
    assert node not in cluster.nodes


# vswitch --------------------------------------------------------------------


def test_vswitch_configure(tmp_path, monkeypatch):
    cluster = Cluster(nodes=[_node()], name="c")
    state = _state(tmp_path, cluster)
    answers = iter(["4000", "10.20.0.0/24"])
    monkeypatch.setattr(screens.prompts, "ask_text", lambda *a, **k: next(answers))

    menu = screens.vswitch_menu(state)
    _find(menu, "Configure vSwitch").run()

    assert cluster.vswitch == Vswitch(vlan_id=4000, subnet="10.20.0.0/24")
    assert definition.load(state.ctx.paths.defn).vswitch.vlan_id == 4000


def test_vswitch_disable(tmp_path, monkeypatch):
    cluster = Cluster(
        nodes=[_node()], name="c", vswitch=Vswitch(vlan_id=4000, subnet="10.10.0.0/24")
    )
    state = _state(tmp_path, cluster)
    monkeypatch.setattr(screens, "confirm", lambda *a, **k: True)

    menu = screens.vswitch_menu(state)
    assert _find(menu, "Disable vSwitch").run() is STAY
    assert cluster.vswitch is None
    assert definition.load(state.ctx.paths.defn).vswitch is None


def test_vswitch_menu_shape_toggles(tmp_path):
    cluster = Cluster(nodes=[_node()], name="c")
    state = _state(tmp_path, cluster)
    labels = [it.label for it in screens.vswitch_menu(state).items()]
    assert "Configure vSwitch" in labels
    assert "Disable vSwitch" not in labels

    cluster.vswitch = Vswitch(vlan_id=4000, subnet="10.10.0.0/24")
    labels = [it.label for it in screens.vswitch_menu(state).items()]
    assert any(label.startswith("Reconfigure vSwitch") for label in labels)
    assert "Disable vSwitch" in labels


# add node -------------------------------------------------------------------


def test_add_node_full_flow(tmp_path, monkeypatch):
    cluster = Cluster(nodes=[], name="c")
    state = _state(tmp_path, cluster)
    texts = iter(["worker1", "10.0.0.2", "10.0.0.254", ""])
    monkeypatch.setattr(screens.prompts, "ask_role", lambda *a, **k: NodeRole.WORKER)
    monkeypatch.setattr(screens.prompts, "ask_text", lambda *a, **k: next(texts))
    monkeypatch.setattr(screens.prompts, "ask_cpu", lambda *a, **k: CpuVendor.INTEL)
    monkeypatch.setattr(screens.prompts, "ask_profile", lambda *a, **k: ProfileKey.GENERIC)
    monkeypatch.setattr(
        screens.prompts, "ask_install_target", lambda *a, **k: InstallTarget(disk="/dev/sda")
    )
    monkeypatch.setattr(screens.prompts, "ask_extra_patches", lambda *a, **k: [])

    _find(screens.nodes_menu(state), "Add node").run()

    assert [n.name for n in cluster.nodes] == ["worker1"]
    added = cluster.nodes[0]
    assert added.cpu is CpuVendor.INTEL
    assert added.install.disk == "/dev/sda"
    assert definition.load(state.ctx.paths.defn).nodes[0].name == "worker1"


def test_add_node_cancel_aborts(tmp_path, monkeypatch):
    cluster = Cluster(nodes=[], name="c")
    state = _state(tmp_path, cluster)
    monkeypatch.setattr(screens.prompts, "ask_role", lambda *a, **k: None)

    _find(screens.nodes_menu(state), "Add node").run()
    assert cluster.nodes == []


def test_add_node_assigns_vlan_ip(tmp_path, monkeypatch):
    cluster = Cluster(nodes=[], name="c", vswitch=Vswitch(vlan_id=4000, subnet="10.10.0.0/24"))
    state = _state(tmp_path, cluster)
    texts = iter(["cp1", "10.0.0.2", "10.0.0.254", ""])
    monkeypatch.setattr(screens.prompts, "ask_role", lambda *a, **k: NodeRole.CONTROLPLANE)
    monkeypatch.setattr(screens.prompts, "ask_text", lambda *a, **k: next(texts))
    monkeypatch.setattr(screens.prompts, "ask_cpu", lambda *a, **k: CpuVendor.AMD)
    monkeypatch.setattr(screens.prompts, "ask_profile", lambda *a, **k: ProfileKey.GENERIC)
    monkeypatch.setattr(
        screens.prompts, "ask_install_target", lambda *a, **k: InstallTarget(disk="/dev/sda")
    )
    monkeypatch.setattr(screens.prompts, "ask_extra_patches", lambda *a, **k: [])

    _find(screens.nodes_menu(state), "Add node").run()
    assert cluster.nodes[0].vlan_ip == "10.10.0.1"  # cp from .1


# main menu items ------------------------------------------------------------


def test_set_cluster_name_only_when_unset(tmp_path, monkeypatch):
    cluster = Cluster(nodes=[_node()])
    state = _state(tmp_path, cluster)
    labels = [it.label for it in screens.main_menu(state).items()]
    assert "Set cluster name" in labels

    monkeypatch.setattr(screens.prompts, "ask_text", lambda *a, **k: "mycluster")
    _find(screens.main_menu(state), "Set cluster name").run()
    assert cluster.name == "mycluster"
    assert "Set cluster name" not in [it.label for it in screens.main_menu(state).items()]


def test_refresh_swaps_snapshot(tmp_path, monkeypatch):
    cluster = Cluster(nodes=[_node()], name="c")
    state = _state(tmp_path, cluster)
    sentinel = _snap({"cp1": NodeState.MAINTENANCE}, k8s=True)
    monkeypatch.setattr(screens.observe, "snapshot", lambda *a, **k: sentinel)

    assert _find(screens.main_menu(state), "Refresh cluster state").run() is STAY
    assert state.snap is sentinel


def test_main_menu_always_has_enabled_item(tmp_path):
    cluster = Cluster(nodes=[])  # maximally broken topology
    state = _state(tmp_path, cluster)
    items = screens.main_menu(state).items()
    assert any(not it.disabled for it in items)


def test_nodes_menu_lists_nodes(tmp_path):
    cluster = Cluster(nodes=[_node(), _node("worker1", role=NodeRole.WORKER)], name="c")
    state = _state(tmp_path, cluster)
    labels = [it.label for it in screens.nodes_menu(state).items()]
    assert labels[:2] == ["cp1", "worker1"]
    assert labels[-2:] == ["Add node", "Back"]


def test_node_submenu_entry_has_node_line_hint(tmp_path):
    node = _node()
    cluster = Cluster(nodes=[node], name="c")
    state = _state(tmp_path, cluster)
    entry = _find(screens.nodes_menu(state), "cp1")
    assert isinstance(entry, Item)
    assert entry.hint is not None  # node_line summary
