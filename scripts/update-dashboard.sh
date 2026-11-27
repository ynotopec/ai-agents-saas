#!/usr/bin/env bash
# Upgrade an existing dashboard using the current kubectl context.
set -euo pipefail

check=false
case "${1:-}" in
  "") ;;
  --check) check=true ;;
  *) echo "Usage: $0 [--check]" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo "Usage: $0 [--check]" >&2; exit 2; }

root=$(cd "$(dirname "$0")/.." && pwd)
kubectl_bin=${KUBECTL:-kubectl}
context=$("$kubectl_bin" config current-context)
kube=("$kubectl_bin" --context "$context" --namespace demo1)
echo "Dashboard agents-saas — contexte $context, namespace demo1"

# Render once so validation and application always use the same bytes.
bundle=$(mktemp)
trap 'rm -f "$bundle"' EXIT
"$kubectl_bin" kustomize "$root" > "$bundle"
"${kube[@]}" get deployment agents-saas -o name >/dev/null
"${kube[@]}" get configmap agents-saas-release -o json |
  python3 -c 'import json, sys; json.loads(json.load(sys.stdin)["data"]["hermes-images.json"])'
"${kube[@]}" get secret agents-saas-secrets -o json |
  python3 -c '
import base64, json, sys
data = json.load(sys.stdin).get("data", {})
for key in ("DEPLOY_TOKEN", "LLM_API_KEY"):
    if not base64.b64decode(data.get(key, ""), validate=True).strip():
        sys.exit("Valeur absente ou vide dans agents-saas-secrets : " + key)
'
"${kube[@]}" apply --dry-run=server -f "$bundle"
if "$check"; then
  exit 0
fi

"${kube[@]}" apply -f "$bundle"
if ! "${kube[@]}" rollout status deployment/agents-saas --timeout=5m; then
  echo "Dashboard non prêt. Examiner kubectl describe deployment/agents-saas et les logs des nouveaux pods dans demo1 avant de relancer." >&2
  exit 1
fi
