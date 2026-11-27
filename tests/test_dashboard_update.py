import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/update-dashboard.sh"


def test_kustomize_scopes_update_and_versions_code(tmp_path):
    kubectl = shutil.which("kubectl")
    if not kubectl:
        pytest.skip("kubectl is required to render the dashboard")
    for path in ("kustomization.yaml", "app.py", "hermes_runtime.py", "release_policy.py"):
        shutil.copy(ROOT / path, tmp_path / path)
    shutil.copytree(ROOT / "manifests", tmp_path / "manifests")

    def render():
        return subprocess.check_output([kubectl, "kustomize", tmp_path])

    original = render()
    assert original == render()
    def config_name(output):
        resources = list(yaml.safe_load_all(output))
        assert sorted(r["kind"] for r in resources) == sorted(
            ["ConfigMap", "Deployment", "Role", "RoleBinding", "ServiceAccount"])
        assert {r["metadata"]["namespace"] for r in resources} == {"demo1"}
        objects = {r["kind"]: r for r in resources}
        dep = objects["Deployment"]
        assert dep["metadata"]["name"] == "agents-saas"
        name = objects["ConfigMap"]["metadata"]["name"]
        volumes = dep["spec"]["template"]["spec"]["volumes"]
        assert {v["configMap"]["name"] for v in volumes if "configMap" in v} == {name, "agents-saas-release"}
        return name

    before = config_name(original)
    with (tmp_path / "app.py").open("a") as app:
        app.write("\n# changed\n")
    assert config_name(render()) != before


@pytest.mark.parametrize("mode,args,success,writes", [
    ("ok", [], True, True), ("ok", ["--check"], True, False),
    ("empty-secret", [], False, False), ("reject", [], False, False),
    ("rollout-failure", [], False, True),
])
def test_update_control_flow(tmp_path, mode, args, success, writes):
    fake = tmp_path / "kubectl"
    log = tmp_path / "calls"
    fake.write_text("""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as f:
    print(json.dumps(args), file=f)
mode = os.environ['MODE']
if args == ['config', 'current-context']:
    print('test-cluster')
elif args[0] == 'kustomize':
    print('test-bundle')
else:
    assert args[:4] == ['--context', 'test-cluster', '--namespace', 'demo1']
    command = args[4:]
    if command[:2] == ['get', 'configmap']:
        print(json.dumps({'data': {'hermes-images.json': '{}'}}))
    if command[:2] == ['get', 'secret']:
        print(json.dumps({'data': {'DEPLOY_TOKEN': '' if mode == 'empty-secret' else 'eA==', 'LLM_API_KEY': 'eA=='}}))
    if command[0] == 'apply':
        assert open(command[-1]).read() == 'test-bundle\\n'
        if '--dry-run=server' in command and mode == 'reject':
            sys.exit(1)
    if command[0] == 'rollout' and mode == 'rollout-failure':
        sys.exit(1)
""")
    fake.chmod(0o755)
    run = subprocess.run([SCRIPT, *args], cwd=tmp_path, capture_output=True,
                         env=dict(os.environ, KUBECTL=str(fake), CALLS=str(log), MODE=mode))
    assert (run.returncode == 0) == success, run.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    applied = [c for c in calls if 'apply' in c and '--dry-run=server' not in c]
    assert bool(applied) == writes
    if writes:
        assert calls.index(applied[0]) > next(i for i, c in enumerate(calls) if '--dry-run=server' in c)
    assert all('restart' not in call for call in calls)
