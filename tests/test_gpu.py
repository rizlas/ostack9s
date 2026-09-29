from collections import Counter
from types import SimpleNamespace

from ostack9s.gpu import count_gpus, gpu_label, gpu_text, gpus_from_specs


def test_pci_alias_and_resources():
    assert gpus_from_specs({"pci_passthrough:alias": "gpu_a100:2"}) == Counter(A100=2)
    assert gpus_from_specs({"pci_passthrough:alias": "gpu_a30:1,sriov_nic:1"}) == Counter(A30=1)
    assert gpus_from_specs({"resources:VGPU": "1"}) == Counter(VGPU=1)
    assert gpus_from_specs({"resources:CUSTOM_GPU_A30": "2"}) == Counter(A30=2)


def test_non_gpu_specs_are_ignored():
    assert not gpus_from_specs({"pci_passthrough:alias": "intel_x710:1"})
    assert not gpus_from_specs({"hw:cpu_policy": "dedicated"})
    assert not gpus_from_specs(None)


def test_count_and_text():
    servers = [
        SimpleNamespace(flavor={"extra_specs": {"pci_passthrough:alias": "gpu_a100:1"}}),
        SimpleNamespace(flavor={"extra_specs": {"pci_passthrough:alias": "gpu_a30:2"}}),
        SimpleNamespace(flavor={"extra_specs": {}}),
    ]
    gpus = count_gpus(servers)
    assert gpus == Counter(A100=1, A30=2)
    assert gpu_text(gpus) == "A100×1, A30×2"
    assert gpu_label("gpu_a100") == "A100"
