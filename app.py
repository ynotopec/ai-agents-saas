#!/usr/bin/env python3
"""Simple FastAPI server for agents-saas dashboard. Uses K8s REST API directly."""
import os
import json
import re
import secrets
from threading import BoundedSemaphore
from urllib.error import HTTPError
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, SecretStr
from hermes_runtime import render_deployment, runtime_settings

class DeployRequest(BaseModel):
    subdomain: str
    password: SecretStr | None = None
    password_confirmation: SecretStr | None = None

class ChangePasswordRequest(BaseModel):
    subdomain: str
    new_password: str

@asynccontextmanager
async def lifespan(app):
    if not DEPLOY_TOKEN.strip():
        raise RuntimeError("DEPLOY_TOKEN is required")
    yield


app = FastAPI(title="Agent SaaS Dashboard", lifespan=lifespan)
browser_auth = HTTPBasic(auto_error=False)
NAMESPACE = "demo1"
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

# Env vars for configurable LLM settings (defaults from local config)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api-nothink.ailab.infocepo.com/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "infocepo-alias")
LLM_MODEL = os.environ.get("LLM_MODEL", "ai-thinking")
DEPLOY_TOKEN = os.environ.get("DEPLOY_TOKEN", "")
HERMES_WEBUI_PASSWORD = os.environ.get("HERMES_WEBUI_PASSWORD", "")
SUBDOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")

def require_deploy_token(request: Request, x_deploy_token: str | None = Header(default=None),
                         credentials: HTTPBasicCredentials | None = Depends(browser_auth)) -> None:
    token = x_deploy_token
    if not token and credentials and credentials.username == "admin":
        if request.method != "GET" and request.headers.get("X-Dashboard-Request") != "1":
            raise HTTPException(status_code=403, detail="Missing dashboard request header")
        token = credentials.password
    if not DEPLOY_TOKEN.strip() or not token or not secrets.compare_digest(token.encode(), DEPLOY_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="Dashboard authentication required",
                            headers={"WWW-Authenticate": 'Basic realm="Agents SaaS"'})


def validate_subdomain(subdomain: str) -> str:
    """Return a Kubernetes/DNS-safe subdomain or raise ValueError."""
    normalized = subdomain.strip()
    if not SUBDOMAIN_RE.fullmatch(normalized):
        raise ValueError(
            "Subdomain must be 1-63 characters and contain only lowercase "
            "letters, numbers, and single hyphens between characters."
        )
    if "--" in normalized:
        raise ValueError("Subdomain must not contain consecutive hyphens.")
    return normalized

def get_token():
    try:
        with open(SA_TOKEN_PATH) as f:
            return f.read().strip()
    except OSError:
        return None

def _api_base(resource_type):
    api_map = {
        "deployments": "apps/v1",
        "ingresses": "networking.k8s.io/v1",
        "certificates": "cert-manager.io/v1",
    }
    version = api_map.get(resource_type.split("/", 1)[0], "v1")
    if version == "v1":
        return "https://kubernetes.default.svc/api/v1"
    parts = version.split("/")
    group = parts[0]
    ver = parts[1]
    return f"https://kubernetes.default.svc/apis/{group}/{ver}"

def k8s_get(resource, label_selector=None):
    import urllib.request
    try:
        token = get_token()
        if not token:
            raise RuntimeError("Kubernetes service account token is unavailable")
        base = _api_base(resource)
        url = f"{base}/namespaces/{NAMESPACE}/{resource}"
        if label_selector:
            url += f"?labelSelector={label_selector}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/json")
        with urllib.request.urlopen(req, cafile=CA_PATH) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Cannot read {resource}") from exc

def k8s_post(resource_type, body):
    import urllib.request
    try:
        token = get_token()
        if not token:
            raise RuntimeError("Kubernetes service account token is unavailable")
        data = json.dumps(body).encode()
        base = _api_base(resource_type)
        url = f"{base}/namespaces/{NAMESPACE}/{resource_type}"
        req = urllib.request.Request(url, data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, cafile=CA_PATH) as resp:
            resp.read()
        return True
    except HTTPError as exc:
        if resource_type == "configmaps" and exc.code == 409:
            raise HTTPException(status_code=409, detail="Subdomain already reserved") from exc
        raise RuntimeError(f"Failed to create {resource_type}: {exc}") from exc
    except Exception as e:
        raise RuntimeError(f"Failed to create {resource_type}: {e}") from e


def k8s_patch(resource_type, name, body):
    """Patch an existing K8s resource."""
    import urllib.request
    try:
        token = get_token()
        if not token:
            raise RuntimeError("Kubernetes service account token is unavailable")
        data = json.dumps(body).encode()
        base = _api_base(resource_type)
        url = f"{base}/namespaces/{NAMESPACE}/{resource_type}/{name}"
        # Use merge-patch for deployments (strategic merge doesn't work via REST API for simple patches)
        content_type = "application/merge-patch+json"
        req = urllib.request.Request(url, data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
            method="PATCH")
        with urllib.request.urlopen(req, cafile=CA_PATH) as resp:
            return json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"Failed to patch {resource_type}/{name}: {e}") from e

def list_instances():
    res = k8s_get("pods", label_selector="app=agent-instance")
    # Deduplicate by agent-instance subdomain label: keep at most one pod
    # per subdomain, preferring Running pods over other phases.
    best: dict[str, dict] = {}
    for pod in res.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("app") != "agent-instance":
            continue
        name = pod["metadata"]["name"]
        name_parts = name.replace("agent-", "").split("-", 1)
        subdomain = labels.get("agent-instance") or (name_parts[1] if len(name_parts) > 1 else name)
        status = pod.get("status", {}).get("phase", "Unknown")
        entry = {
            "name": name,
            "subdomain": subdomain,
            "url": f"https://{subdomain}.ailab.infocepo.com",
            "status": status
        }
        # Prefer Running over any other phase
        if subdomain not in best or status == "Running":
            best[subdomain] = entry
    return list(best.values())

def build_config() -> str:
    """Generate hermes config.yaml from env vars or defaults."""
    nothink_api_key = os.environ.get("NOTHINK_API_KEY", "")
    nothink_api_url = os.environ.get("NOTHINK_API_URL", "https://api-nothink.ailab.infocepo.com/v1")
    toolsets = os.environ.get("HERMES_TOOLSETS", "hermes-cli")
    model = os.environ.get("LLM_MODEL", LLM_MODEL)
    provider = os.environ.get("LLM_PROVIDER", LLM_PROVIDER)
    base_url = os.environ.get("LLM_BASE_URL", LLM_BASE_URL)
    api_key = os.environ.get("LLM_API_KEY", LLM_API_KEY)
    context_length = os.environ.get("LLM_CONTEXT_LENGTH", "262144")
    return (
        f"model:\n"
        f"  default: {model}\n"
        f"  provider: {provider}\n"
        f"  context_length: {context_length}\n"
        f"  base_url: {base_url}\n"
        f"  api_key: {json.dumps(api_key)}\n"
        f"providers:\n"
        f"  ai-nothink:\n"
        f"    name: ai-nothink\n"
        f"    type: openai\n"
        f"    api_url: {nothink_api_url}\n"
        f"    api_key: {json.dumps(nothink_api_key)}\n"
        f"fallback_providers: []\n"
        f"custom_providers:\n"
        f"  - name: {provider}\n"
        f"    base_url: {base_url}\n"
        f"    api_key: {json.dumps(api_key)}\n"
        f"    model: {model}\n"
        f"toolsets:\n"
        f"  - {toolsets}\n"
        f"web:\n"
        f"  search_backend: ddgs\n"
        f"  extract_backend: trafilatura\n"
    )

def deploy_instance(subdomain: str, password: str | None = None,
                    password_confirmation: str | None = None) -> dict:
    subdomain = validate_subdomain(subdomain)
    # A custom password, entered twice, is mandatory for every new instance.
    if not password:
        raise ValueError("Un mot de passe est obligatoire pour déployer une instance")
    if len(password) < 4:
        raise ValueError("Le mot de passe doit contenir au moins 4 caractères")
    if not password_confirmation:
        raise ValueError("Confirmez le mot de passe")
    if password != password_confirmation:
        raise ValueError("Les mots de passe ne correspondent pas")
    hash8 = os.urandom(4).hex()  # 32-bit random, 8 hex chars
    name = f"agent-{hash8}-{subdomain}"
    domain = f"{subdomain}.ailab.infocepo.com"

    try:
        runtime_settings()
        if len(subdomain) > 44 or subdomain.endswith("-api") or subdomain == "agents-saas":
            raise HTTPException(status_code=400, detail="Reserved or oversized subdomain")
        # Older instances predate reservations, including scaled-to-zero deployments.
        if k8s_get("deployments", label_selector=f"agent-instance={subdomain}")["items"]:
            raise HTTPException(status_code=409, detail="Subdomain already deployed")
        hosts = {domain, f"{subdomain}-api.ailab.infocepo.com"}
        if any(rule.get("host") in hosts for ing in k8s_get("ingresses")["items"]
               for rule in ing.get("spec", {}).get("rules", [])):
            raise HTTPException(status_code=409, detail="Ingress host already assigned")
        k8s_post("configmaps", {"apiVersion": "v1", "kind": "ConfigMap",
                 "metadata": {"name": f"agent-domain-{subdomain}", "namespace": NAMESPACE},
                 "data": {"owner": name}})
        k8s_post("secrets", {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": f"{name}-runtime-secrets", "namespace": NAMESPACE},
            "type": "Opaque",
            "stringData": {"password": password, "API_SERVER_KEY": secrets.token_urlsafe(32)},
        })
        config = build_config()

        # Data PVC: 20Gi, Workspace PVC: 5Gi
        pvc_specs = [
            (f"{name}-data", "20Gi"),
            (f"{name}-workspace", "5Gi"),
        ]
        for pvc_name, storage in pvc_specs:
            k8s_post("persistentvolumeclaims", {
                "apiVersion": "v1", "kind": "PersistentVolumeClaim",
                "metadata": {"name": pvc_name, "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
                "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "microk8s-hostpath",
                         "resources": {"requests": {"storage": storage}}}
            })

        k8s_post("secrets", {
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": f"{name}-config", "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
            "stringData": {"config.yaml": config, "LLM_API_KEY": LLM_API_KEY}
        })

        deployment_body = render_deployment(
            name, NAMESPACE, subdomain, hash8, secret_name=f"{name}-runtime-secrets",
            llm_env={"LLM_BASE_URL": LLM_BASE_URL,
                     "LLM_PROVIDER": LLM_PROVIDER, "LLM_MODEL": LLM_MODEL},
        )["items"][0]
        k8s_post("deployments", deployment_body)

        k8s_post("services", {
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": f"{name}-svc", "namespace": NAMESPACE, "labels": {"app": "agent-instance"}},
            "spec": {"ports": [{"name": "webui", "port": 80, "targetPort": 8787},
                               {"name": "gateway", "port": 8642, "targetPort": 8642}],
                     "selector": {"app": "agent-instance", "agent-instance": subdomain, "agent-hash": hash8}}
        })

        if os.environ.get("EXPOSE_GATEWAY_INGRESS", "").lower() == "true":
            # Gateway ingress for port 8642 (API gateway)
            api_domain = f"{subdomain}-api.ailab.infocepo.com"
            k8s_post("ingresses", {
                "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
                "metadata": {"name": f"{name}-gateway-ingress", "namespace": NAMESPACE,
                             "annotations": {"cert-manager.io/cluster-issuer": "letsencrypt-prod",
                                             "nginx.ingress.kubernetes.io/ssl-redirect": "true"}},
                "spec": {
                    "ingressClassName": "public",
                    "tls": [{"hosts": [api_domain], "secretName": f"{name}-gateway-tls"}],
                    "rules": [{"host": api_domain, "http": {
                        "paths": [{"path": "/", "pathType": "Prefix",
                                  "backend": {"service": {"name": f"{name}-svc", "port": {"number": 8642}}}}]
                    }}]
                }
            })

        k8s_post("ingresses", {
            "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
            "metadata": {"name": f"{name}-ingress", "namespace": NAMESPACE,
                         "annotations": {"cert-manager.io/cluster-issuer": "letsencrypt-prod",
                                         "nginx.ingress.kubernetes.io/ssl-redirect": "true"}},
            "spec": {
                "ingressClassName": "public",
                "tls": [{"hosts": [domain], "secretName": f"{name}-tls"}],
                "rules": [{"host": domain, "http": {"paths": [{"path": "/", "pathType": "Prefix",
                        "backend": {"service": {"name": f"{name}-svc", "port": {"number": 80}}}}]}}]
            }
        })

        return {"success": True, "name": name, "subdomain": subdomain, "hash": hash8,
                "url": f"https://{subdomain}.ailab.infocepo.com"}
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}

@app.get("/", response_class=HTMLResponse)
def index(_: None = Depends(require_deploy_token)):
    return HTMLResponse(CONTENT)

@app.get("/api/instances")
def api_instances(_: None = Depends(require_deploy_token)):
    return JSONResponse(list_instances())

@app.post("/api/deploy")
def api_deploy(req: DeployRequest, _: None = Depends(require_deploy_token)):
    try:
        result = deploy_instance(req.subdomain,
                                 req.password.get_secret_value() if req.password else None,
                                 req.password_confirmation.get_secret_value() if req.password_confirmation else None)
        if "error" in result:
            raise HTTPException(status_code=502, detail=result["error"])
        return JSONResponse(result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise

def change_password(subdomain: str, new_password: str) -> dict:
    """Rotate a Secret-backed WebUI password using upstream native hashing."""
    if not new_password or len(new_password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    subdomain = validate_subdomain(subdomain)

    deployments = k8s_get("deployments", label_selector=f"app=agent-instance,agent-instance={subdomain}")["items"]
    if not deployments:
        raise HTTPException(status_code=404, detail="Unknown instance")
    if len(deployments) != 1:
        raise HTTPException(status_code=409, detail="Ambiguous instance")
    deployment = deployments[0]
    deployment_name = deployment["metadata"]["name"]
    containers = deployment.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    webui = next((c for c in containers if c.get("name") == "hermes-webui"), None)
    if webui is None:
        raise HTTPException(status_code=502, detail="Cannot find WebUI deployment")
    secret_name = f"{deployment_name}-password-{secrets.token_hex(4)}"
    k8s_post("secrets", {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": NAMESPACE},
        "type": "Opaque", "stringData": {"password": new_password},
    })
    webui["env"] = [e for e in webui.get("env", []) if e["name"] != "HERMES_WEBUI_PASSWORD"] + [
        {"name": "HERMES_WEBUI_PASSWORD", "valueFrom":
         {"secretKeyRef": {"name": secret_name, "key": "password"}}}]
    rollout_body = {"spec": {"template": {"spec": {"containers": containers},
                    "metadata": {"annotations": {"password-changed-at": datetime.now().isoformat()}}}}}
    version = deployment.get("metadata", {}).get("resourceVersion")
    if version:
        rollout_body["metadata"] = {"resourceVersion": version}
    k8s_patch("deployments", deployment_name, rollout_body)

    return {"success": True, "subdomain": subdomain}


@app.post("/api/change-password")
def api_change_password(req: ChangePasswordRequest, _: None = Depends(require_deploy_token)):
    try:
        return JSONResponse(change_password(req.subdomain, req.new_password))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


class WebExtractRequest(BaseModel):
    urls: list[str] = Field(max_length=8)


@app.get("/health")
def health():
    return {"status": "ok"}


extract_slots = BoundedSemaphore(2)
MAX_PAGE_BYTES = 1024 * 1024


@app.post("/api/web-extract")
def api_web_extract(req: WebExtractRequest, _: None = Depends(require_deploy_token)):
    import httpx
    import trafilatura

    proxy = os.environ.get("WEB_EXTRACT_PROXY")
    if not proxy:
        raise HTTPException(status_code=503, detail="Extraction requires a filtering egress proxy")
    for url in req.urls:
        try:
            parts = urlsplit(url)
            valid = (len(url) <= 2048 and parts.scheme == "https" and parts.hostname
                     and not parts.username and not parts.password and parts.port in (None, 443))
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(status_code=400, detail="Expected HTTPS URLs on port 443")
    if not extract_slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="Extractor busy")
    try:
        with httpx.Client(proxy=proxy, timeout=10, trust_env=False, follow_redirects=False,
                          headers={"Accept-Encoding": "identity"}) as client:
            return extract_pages(client, req.urls, trafilatura.extract)
    finally:
        extract_slots.release()


def extract_pages(client, urls, extract):
    import httpx
    from time import monotonic

    results = []
    deadline = monotonic() + 30
    for url in urls:
        content = None
        extractor = "trafilatura-inline"
        try:
            if monotonic() < deadline:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    if resp.headers.get("Content-Encoding", "identity").lower() == "identity":
                        page = bytearray()
                        for chunk in resp.iter_raw():
                            if len(page) + len(chunk) > MAX_PAGE_BYTES or monotonic() >= deadline:
                                break
                            page.extend(chunk)
                        else:
                            content = extract(bytes(page), url=url, include_comments=False,
                                              favor_precision=True, output_format="markdown")
        except (httpx.HTTPError, ValueError):
            pass

        if content and len(content.strip()) > 10:
            results.append({
                "url": url,
                "title": "",
                "content": content.strip(),
                "error": None,
                "metadata": {"extractor": extractor or "unknown", "format": "markdown"},
            })
        else:
            results.append({
                "url": url,
                "title": "",
                "content": "",
                "error": "No extractable content found",
                "metadata": {},
            })
    return {"results": results}

CONTENT = """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agents SaaS Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f5f5; color: #333; }
        .container { max-width: 800px; margin: 0 auto; padding: 2rem; }
        h1 { font-size: 2rem; margin-bottom: 1rem; color: #1a73e8; }
        .card { background: white; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); padding: 2rem; margin-bottom: 1rem; }
        .form-group { margin-bottom: 1rem; }
        label { display: block; margin-bottom: 0.5rem; font-weight: 500; }
        input[type="text"], input[type="password"] { width: 100%; padding: 0.75rem; border: 1px solid #ddd; border-radius: 4px; font-size: 1rem; }
        .password-row { display: flex; gap: 0.5rem; }
        .password-row input { flex: 1; min-width: 0; }
        .password-row button { padding: 0.5rem 0.75rem; }
        .hint { display: block; margin-top: 0.5rem; color: #555; }
        input:focus { outline: none; border-color: #1a73e8; box-shadow: 0 0 0 2px rgba(26,115,232,0.2); }
        button { background: #1a73e8; color: white; padding: 0.75rem 1.5rem; border: none; border-radius: 4px; font-size: 1rem; cursor: pointer; }
        button:hover { background: #1557b0; }
        button:disabled { background: #ccc; cursor: not-allowed; }
        .result { margin-top: 1rem; padding: 1rem; border-radius: 4px; }
        .success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .instances { margin-top: 2rem; }
        .instances h2 { margin-bottom: 1rem; }
        .instance-item { background: #f8f9fa; padding: 1rem; border-radius: 4px; margin-bottom: 0.5rem; display: flex; justify-content: space-between; align-items: center; }
        .instance-item a { color: #1a73e8; text-decoration: none; }
        .instance-item a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 Agents SaaS Dashboard</h1>
        <div class="card">
            <h2>Déployer une nouvelle instance</h2>
            <form id="deploy-form">
                <div class="form-group">
                    <label for="subdomain">Sous-domaine :</label>
                    <input type="text" id="subdomain" name="subdomain" placeholder="ex: mon-projet" required>
                </div>
                <div class="form-group">
                    <label for="instance-password">Mot de passe :</label>
                    <div class="password-row">
                        <input type="password" id="instance-password" autocomplete="new-password"
                               minlength="4" required aria-describedby="password-hint">
                        <button type="button" id="toggle-password" aria-label="Afficher les mots de passe"
                                aria-pressed="false">👁</button>
                    </div>
                    <small class="hint" id="password-hint">Obligatoire (min. 4 caractères).</small>
                </div>
                <div class="form-group" id="password-confirmation-group">
                    <label for="password-confirmation">Confirmer le mot de passe :</label>
                    <input type="password" id="password-confirmation" autocomplete="new-password" minlength="4" required>
                </div>
                <button type="submit" id="deploy-btn">Déployer</button>
            </form>
            <div id="result"></div>
        </div>
        <div class="instances">
            <h2>Instances existantes</h2>
            <div id="instances-list">Chargement...</div>
        </div>
    </div>
    <script>
        const passwordInput = document.getElementById('instance-password');
        const confirmationInput = document.getElementById('password-confirmation');
        const passwordToggle = document.getElementById('toggle-password');
        function validatePasswords() {
            confirmationInput.setCustomValidity(
                confirmationInput.value && confirmationInput.value !== passwordInput.value
                    ? 'Les mots de passe ne correspondent pas' : '');
        }
        passwordInput.addEventListener('input', validatePasswords);
        confirmationInput.addEventListener('input', validatePasswords);
        passwordToggle.addEventListener('click', () => {
            const show = passwordInput.type === 'password';
            passwordInput.type = confirmationInput.type = show ? 'text' : 'password';
            passwordToggle.setAttribute('aria-pressed', String(show));
            passwordToggle.setAttribute('aria-label', show ? 'Masquer les mots de passe' : 'Afficher les mots de passe');
        });

        async function deploy(e) {
            e.preventDefault();
            const subdomain = document.getElementById('subdomain').value;
            validatePasswords();
            if (!passwordInput.value || !confirmationInput.value) {
                (passwordInput.value ? confirmationInput : passwordInput).reportValidity();
                return;
            }
            if (passwordInput.value !== confirmationInput.value) {
                confirmationInput.setCustomValidity('Les mots de passe ne correspondent pas');
                confirmationInput.reportValidity();
                return;
            }
            const payload = { subdomain,
                password: passwordInput.value,
                password_confirmation: confirmationInput.value };
            const btn = document.getElementById('deploy-btn');
            const result = document.getElementById('result');
            btn.disabled = true;
            btn.textContent = 'Déploiement...';
            try {
                const resp = await fetch('/api/deploy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'X-Dashboard-Request': '1' },
                    body: JSON.stringify(payload)
                });
                let data;
                try { data = await resp.json(); } catch { data = { error: 'Bad response' }; }

                const errorMsg = (data.error || data.detail || 'Erreur inconnue').toString();

                if (resp.status === 200) {
                    if (data.success) {
                        result.innerHTML = '<div class="result success">✅ Déployé !<br><a href="' + data.url + '" target="_blank">' + data.url + '</a></div>';
                        document.getElementById('subdomain').value = '';
                        passwordInput.value = confirmationInput.value = '';
                        passwordInput.type = confirmationInput.type = 'password';
                        passwordToggle.setAttribute('aria-pressed', 'false');
                        passwordToggle.setAttribute('aria-label', 'Afficher les mots de passe');
                        loadInstances();
                    } else {
                        result.textContent = "❌ " + errorMsg;
                    }
                } else {
                    result.textContent = "❌ Erreur " + resp.status + " : " + errorMsg;
                }
            } catch (err) {
                result.textContent = "❌ " + err.message;
            } finally {
                btn.disabled = false;
                btn.textContent = 'Déployer';
            }
        }

        async function loadInstances() {
            const list = document.getElementById('instances-list');
            try {
                const resp = await fetch('/api/instances');
                const instances = await resp.json();
                if (instances.length === 0) {
                    list.innerHTML = '<p>Aucune instance.</p>';
                    return;
                }
                list.innerHTML = instances.map(i =>
                    '<div class="instance-item"><strong>' + i.subdomain + '</strong> → <a href="' + i.url + '" target="_blank">' + i.url + '</a></div>'
                ).join('');
            } catch (err) {
                list.innerHTML = '<p>Erreur : ' + err.message + '</p>';
            }
        }

        document.getElementById('deploy-form').addEventListener('submit', deploy);
        loadInstances();
    </script>
</body>
</html>"""

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=8000, workers=1)
