"""Invariants of the local kind manifests (docs/plan.md Day 18, ADR-0014).

Renders them the way k8s_up.sh does (`render.sh TAG`, with an empty env file
so no real key is read) and checks what every rollout depends on:
probes, requests/limits, a real image tag, one Deployment per platform
service and never per app, `outbox-relay` at one replica, and every
ConfigMap/Secret a pod references actually rendered. `kubeconform` validates
the same output against the Kubernetes schemas.

Needs `kubectl` (Kustomize is built in) and `kubeconform` (`brew install
kubeconform`); each test that needs one is skipped without it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

K8S = Path(__file__).resolve().parents[1]
BACKEND = K8S.parents[1]
TAG = "0123456789ab"
PLATFORM = {
    "registry",
    "ingestion",
    "orchestrator",
    "tool-gateway",
    "memory-store",
    "review-console",
    "outbox-relay",
}
APP_IDS = {p.name for p in (BACKEND / "apps").iterdir() if (p / "manifest.yaml").exists()}

@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> str:
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not installed")
    env_file = tmp_path_factory.mktemp("env") / "empty.env"
    env_file.write_text("")
    out = subprocess.run(
        [str(K8S / "render.sh"), TAG],
        env={"PATH": shutil.os.environ["PATH"], "ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


@pytest.fixture(scope="module")
def docs(rendered) -> list[dict]:
    return [d for d in yaml.safe_load_all(rendered) if d]


def of_kind(docs, *kinds) -> list[dict]:
    return [d for d in docs if d["kind"] in kinds]


def workloads(docs) -> list[dict]:
    return of_kind(docs, "Deployment", "StatefulSet")


def containers(workload) -> list[dict]:
    return workload["spec"]["template"]["spec"]["containers"]


def test_everything_is_in_the_platform_namespace(docs):
    for d in docs:
        if d["kind"] == "Namespace":
            assert d["metadata"]["name"] == "agentic-platform"
        else:
            assert d["metadata"]["namespace"] == "agentic-platform", d["metadata"]["name"]


def test_one_deployment_per_platform_service_never_per_app(docs):
    names = {d["metadata"]["name"] for d in of_kind(docs, "Deployment")}
    assert names == PLATFORM | {"otel-collector"}
    assert APP_IDS, "no apps found under backend/apps"
    for name in names:
        assert not any(app_id in name for app_id in APP_IDS), name


def test_each_platform_deployment_has_a_service(docs):
    services = {d["metadata"]["name"] for d in of_kind(docs, "Service")}
    assert PLATFORM <= services


def test_infra_is_single_replica_statefulsets_with_volumes(docs):
    sts = {d["metadata"]["name"]: d for d in of_kind(docs, "StatefulSet")}
    assert set(sts) == {"postgres", "redis", "kafka"}
    for name, s in sts.items():
        assert s["spec"]["replicas"] == 1, name
        assert s["spec"]["volumeClaimTemplates"], name


def test_outbox_relay_runs_one_replica(docs):
    (relay,) = [d for d in of_kind(docs, "Deployment") if d["metadata"]["name"] == "outbox-relay"]
    assert relay["spec"]["replicas"] == 1


def test_every_container_has_probes_and_requests_and_limits(docs):
    for w in workloads(docs):
        for c in containers(w):
            where = f"{w['metadata']['name']}/{c['name']}"
            assert "livenessProbe" in c and "readinessProbe" in c, where
            for bound in ("requests", "limits"):
                assert {"cpu", "memory"} <= set(c["resources"][bound]), f"{where} {bound}"


def test_platform_probes_are_healthz_and_readyz(docs):
    for d in of_kind(docs, "Deployment"):
        if d["metadata"]["name"] not in PLATFORM:
            continue
        (c,) = containers(d)
        assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"


def test_no_image_is_latest_and_platform_images_carry_the_tag(docs):
    jobs = of_kind(docs, "Job")
    pods = workloads(docs) + jobs
    for w in pods:
        for c in containers(w):
            repo, _, tag = c["image"].rpartition(":")
            assert repo and tag and tag != "latest", c["image"]
            if repo.startswith("agentic-platform/"):
                assert tag == TAG, c["image"]
                assert repo.removeprefix("agentic-platform/") == w["metadata"]["name"]


def test_no_service_links(docs):
    # apache/kafka reads every KAFKA_* env var as config; service links
    # (KAFKA_PORT=tcp://...) would break it, so they're off everywhere.
    for w in workloads(docs) + of_kind(docs, "Job"):
        assert w["spec"]["template"]["spec"]["enableServiceLinks"] is False, w["metadata"]["name"]


def test_only_orchestrator_gets_the_llm_secret(docs):
    for d in of_kind(docs, "Deployment"):
        refs = [e for c in containers(d) for e in c.get("envFrom", []) if "secretRef" in e]
        if d["metadata"]["name"] == "orchestrator":
            assert [r["secretRef"]["name"].startswith("llm-api-key-") for r in refs] == [True]
        else:
            assert refs == [], d["metadata"]["name"]


def test_every_referenced_configmap_and_secret_is_rendered(docs):
    have = {(d["kind"], d["metadata"]["name"]) for d in of_kind(docs, "ConfigMap", "Secret")}
    for w in workloads(docs) + of_kind(docs, "Job"):
        spec = w["spec"]["template"]["spec"]
        refs = []
        for v in spec.get("volumes", []):
            if "configMap" in v:
                refs.append(("ConfigMap", v["configMap"]["name"]))
        for c in spec["containers"]:
            for e in c.get("envFrom", []):
                if "configMapRef" in e:
                    refs.append(("ConfigMap", e["configMapRef"]["name"]))
                if "secretRef" in e:
                    refs.append(("Secret", e["secretRef"]["name"]))
        for ref in refs:
            assert ref in have, f"{w['metadata']['name']} references missing {ref}"


def test_topics_job_uses_six_partitions(docs):
    (job,) = of_kind(docs, "Job")
    env = {e["name"]: e["value"] for e in containers(job)[0]["env"]}
    assert env["PARTITIONS"] == "6"
    assert env["BOOTSTRAP"] == "kafka:9092"


def test_platform_config_matches_service_names(docs):
    (cfg,) = [d for d in of_kind(docs, "ConfigMap") if d["metadata"]["name"] == "platform-config"]
    data = cfg["data"]
    assert data["KAFKA_BOOTSTRAP_SERVERS"] == "kafka:9092"
    assert data["REGISTRY_URL"] == "http://registry:8000"
    assert data["TOOL_GATEWAY_URL"] == "http://tool-gateway:8000/mcp"
    assert data["MEMORY_STORE_TARGET"] == "memory-store:50051"
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in data  # not until Day 23


@pytest.mark.skipif(shutil.which("kubeconform") is None, reason="kubeconform not installed")
def test_kubeconform_validates_rendered_output(rendered, tmp_path):
    out = subprocess.run(
        ["kubeconform", "-strict", "-summary", "-cache", str(tmp_path), "-"],
        input=rendered,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr


def test_env_file_quotes_are_stripped_like_compose(tmp_path):
    # Compose's env_file drops surrounding quotes; secretGenerator doesn't.
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not installed")
    import base64

    env_file = tmp_path / "quoted.env"
    env_file.write_text(
        '# a comment\nOPENAI_API_KEY="sk-double"\nOTHER=\'single\'\nPLAIN=bare\n\n'
    )
    out = subprocess.run(
        [str(K8S / "render.sh"), TAG],
        env={"PATH": shutil.os.environ["PATH"], "ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (secret,) = [d for d in yaml.safe_load_all(out) if d and d["kind"] == "Secret"]
    data = {k: base64.b64decode(v).decode() for k, v in secret["data"].items()}
    assert data == {"OPENAI_API_KEY": "sk-double", "OTHER": "single", "PLAIN": "bare"}
