#!/bin/bash
# deploy-agent.sh - Create a new agent instance with all K8s resources
set -euo pipefail

SUBDOMAIN="${1:?Usage: deploy-agent.sh <subdomain>}"
NAMESPACE="${2:-demo1}"
KUBECONFIG="${3:-/etc/kubernetes/admin.conf}"
DOMAIN_SUFFIX=".ailab.infocepo.com"
CLUSTER_ISSUER="letsencrypt-prod"
INGRESS_CLASS="public"

if ! [[ "$SUBDOMAIN" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]] || [[ "$SUBDOMAIN" == *--* || "$SUBDOMAIN" == *-api || "$SUBDOMAIN" == agents-saas || ${#SUBDOMAIN} -gt 44 ]]; then
  echo "Invalid subdomain: expected a non-reserved DNS label of at most 44 characters" >&2
  exit 2
fi
: "${LLM_API_KEY:?Set LLM_API_KEY in the environment}"
: "${HERMES_WEBUI_PASSWORD:?Set HERMES_WEBUI_PASSWORD in the environment}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Validate the immutable release pair before touching the cluster.
PYTHONPATH="$SCRIPT_DIR" python3 -c 'from hermes_runtime import runtime_settings; runtime_settings()'

# Generate unique prefix
HASH=$(date +%s%N | sha256sum | cut -c1-8)
PREFIX="agent-${HASH}"
NAME="${PREFIX}-${SUBDOMAIN}"
DOMAIN="${SUBDOMAIN}.ailab.infocepo.com"

echo "=== Deploying ${NAME} ==="
echo "Domain: ${DOMAIN}"

kube=(kubectl --kubeconfig="${KUBECONFIG}" -n "${NAMESPACE}")
existing=$("${kube[@]}" get deployments -l "agent-instance=${SUBDOMAIN}" -o name)
hosts=$("${kube[@]}" get ingresses -o 'jsonpath={range .items[*].spec.rules[*]}{.host}{"\n"}{end}')
if [[ -n "$existing" ]] || grep -Fxq "$DOMAIN" <<< "$hosts"; then
  echo "Subdomain already in use" >&2
  exit 1
fi
"${kube[@]}" create configmap "agent-domain-${SUBDOMAIN}" --from-literal=owner="$NAME"

# Create the per-instance WebUI secret without writing credentials to the repository.
python3 -c '
import json, os, secrets, sys
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": sys.argv[1]},
                  "stringData": {"password": os.environ["HERMES_WEBUI_PASSWORD"],
                                 "API_SERVER_KEY": secrets.token_urlsafe(32)}}))
' "${NAME}-webui-secret" | "${kube[@]}" create -f -

# Create PVCs
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${NAME}-data
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: microk8s-hostpath
  resources:
    requests:
      storage: 20Gi
  volumeMode: Filesystem
EOF

cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${NAME}-workspace
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: microk8s-hostpath
  resources:
    requests:
      storage: 20Gi
  volumeMode: Filesystem
EOF

# Store the provider credentials only in a Secret.
llm_key_yaml=$(python3 -c 'import json, os; print(json.dumps(os.environ["LLM_API_KEY"]))')
python3 -c '
import json, os, sys
print(json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": sys.argv[1]},
                  "stringData": {"config.yaml": sys.stdin.read(), "LLM_API_KEY": os.environ["LLM_API_KEY"]}}))
' "${NAME}-config" <<EOF | "${kube[@]}" create -f -
    model:
      default: ai-thinking
      provider: custom
      context_length: ${LLM_CONTEXT_LENGTH:-262144}
      base_url: https://api-nothink.ailab.infocepo.com/v1
      api_key: ${llm_key_yaml}
    providers:
      ai-nothink:
        name: ai-nothink
        type: openai
        api_url: https://api-nothink.ailab.infocepo.com/v1
        api_key: ${llm_key_yaml}
    fallback_providers: []
    toolsets:
      - hermes-cli
    web:
      search_backend: ddgs
      extract_backend: trafilatura
EOF

# Render the deployment using the shared contract.
python3 "$SCRIPT_DIR/scripts/render-hermes-deployment.py" "$NAME" "$NAMESPACE" "$SUBDOMAIN" "$HASH" |
  kubectl --kubeconfig="${KUBECONFIG}" apply -f -

# Create Service
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: v1
kind: Service
metadata:
  name: ${NAME}-svc
  namespace: ${NAMESPACE}
  labels:
    app: agent-instance
spec:
  type: ClusterIP
  ports:
  - port: 80
    targetPort: 8787
    protocol: TCP
    name: http
  selector:
    app: agent-instance
    agent-instance: '${SUBDOMAIN}'
    agent-hash: '${HASH}'
EOF

# Create Ingress
cat <<EOF | kubectl --kubeconfig="${KUBECONFIG}" apply -f -
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${NAME}-ingress
  namespace: ${NAMESPACE}
  annotations:
    cert-manager.io/cluster-issuer: ${CLUSTER_ISSUER}
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  ingressClassName: ${INGRESS_CLASS}
  tls:
  - hosts:
    - ${DOMAIN}
    secretName: ${NAME}-tls
  rules:
  - host: ${DOMAIN}
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: ${NAME}-svc
            port:
              number: 80
EOF

echo "✅ ${NAME} deployed at https://${DOMAIN}"
