"""
Tests for agents-saas application.
Run: python3 -m pytest tests/test_app.py -v
"""
import pytest
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import app as app_module
from app import app, build_config, validate_subdomain

AUTH_HEADERS = {"X-Deploy-Token": "test-deploy-token"}
# A custom password, entered twice, is mandatory for every new instance.
VALID_PASSWORD = {"password": "chosen-password", "password_confirmation": "chosen-password"}


@pytest.fixture
def client(monkeypatch, release):
    """Test client with test config."""
    monkeypatch.setattr(app_module, "DEPLOY_TOKEN", "test-deploy-token")
    monkeypatch.setattr(app_module, "HERMES_WEBUI_PASSWORD", "test-webui-password")
    monkeypatch.setattr(app_module, "k8s_get", lambda *args, **kwargs: {"items": []})
    monkeypatch.setattr(app_module, "LLM_API_KEY", "test-llm-key")
    monkeypatch.setattr(app_module, "LLM_PROVIDER", "test-provider")
    monkeypatch.setattr(app_module, "LLM_MODEL", "test-model")
    monkeypatch.setattr(app_module, "LLM_BASE_URL", "https://test.example.com/v1")
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        yield client


class TestValidateSubdomain:
    """Tests for subdomain validation."""

    def test_valid_subdomain(self, client):
        """Subdomain should accept valid alphanumeric strings."""
        assert validate_subdomain("test") == "test"
        assert validate_subdomain("my-agent") == "my-agent"
        assert validate_subdomain("agent123") == "agent123"

    def test_uppercase_rejected(self, client):
        """Subdomain should reject uppercase letters."""
        with pytest.raises(ValueError):
            validate_subdomain("Test")

    def test_consecutive_hyphens_rejected(self, client):
        """Subdomain should reject consecutive hyphens."""
        with pytest.raises(ValueError):
            validate_subdomain("test--agent")

    def test_invalid_special_chars(self, client):
        """Subdomain should reject special characters."""
        with pytest.raises(ValueError):
            validate_subdomain("test@agent")

    def test_empty_string(self, client):
        """Empty string should be rejected."""
        with pytest.raises(ValueError):
            validate_subdomain("")


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    def test_health_returns_ok(self, client):
        """GET /health should return status ok."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestInstancesEndpoint:
    """Tests for /api/instances endpoint."""

    def test_instances_returns_list(self, client):
        """GET /api/instances should return a list (may be empty without K8s)."""
        response = client.get("/api/instances", headers=AUTH_HEADERS)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


class TestDeployEndpoint:
    """Tests for /api/deploy endpoint."""

    def test_deploy_requires_authentication(self, client):
        """POST /api/deploy should reject requests without the shared token."""
        response = client.post("/api/deploy", json={"subdomain": "test", **VALID_PASSWORD})
        assert response.status_code == 401

    @patch('app.k8s_post')
    def test_deploy_returns_success(self, mock_k8s_post, client):
        """POST /api/deploy should return success response when K8s calls succeed."""
        response = client.post("/api/deploy", headers=AUTH_HEADERS, json={"subdomain": "test", **VALID_PASSWORD})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["subdomain"] == "test"
        assert "hash" in data
        assert "url" in data
        assert data["url"] == "https://test.ailab.infocepo.com"

    @patch('app.k8s_post')
    def test_deploy_returns_correct_name(self, mock_k8s_post, client):
        """Deploy should return name with format agent-<hash>-<subdomain>."""
        response = client.post("/api/deploy", headers=AUTH_HEADERS, json={"subdomain": "myapp", **VALID_PASSWORD})
        assert response.status_code == 200
        data = response.json()
        assert data["name"].startswith("agent-")
        assert data["name"].endswith("-myapp")

    def test_deploy_invalid_subdomain(self, client):
        """POST /api/deploy with invalid subdomain should return 400."""
        response = client.post("/api/deploy", headers=AUTH_HEADERS, json={"subdomain": "INVALID", **VALID_PASSWORD})
        assert response.status_code == 400

    @patch("app.k8s_post")
    def test_deploy_does_not_mount_service_account_token(self, mock_k8s_post, client):
        """Tenant pods must not receive Kubernetes API credentials."""
        response = client.post("/api/deploy", headers=AUTH_HEADERS, json={"subdomain": "isolated", **VALID_PASSWORD})

        assert response.status_code == 200
        deployment = next(
            call.args[1]
            for call in mock_k8s_post.call_args_list
            if call.args[0] == "deployments"
        )
        assert deployment["spec"]["template"]["spec"]["automountServiceAccountToken"] is False


class TestConfigGeneration:
    """Tests for config.yaml generation."""

    def test_config_uses_env_vars(self):
        """Config should use environment variables, not hardcoded globals."""
        with patch.dict(os.environ, {
            "LLM_BASE_URL": "https://test.example.com/v1",
            "LLM_API_KEY": "test-key",
            "LLM_PROVIDER": "test-provider",
            "LLM_MODEL": "test-model",
        }):
            config = build_config()
            assert "test-model" in config
            assert "test-provider" in config
            assert "https://test.example.com/v1" in config
            assert "test-key" in config

    def test_config_contains_required_sections(self):
        """Config should contain all required sections."""
        config = build_config()
        assert "model:" in config
        assert "providers:" in config
        assert "custom_providers:" in config
        assert "toolsets:" in config


class TestChangePasswordEndpoint:
    """Tests for the POST /api/change-password endpoint."""

    @patch("app.k8s_patch")
    @patch("app.k8s_get")
    @patch("app.k8s_post")
    def test_change_password_creates_secret(self, mock_k8s_post, mock_k8s_get, mock_patch, client):
        """POST /api/change-password should return success when called with valid data."""
        mock_k8s_get.return_value = {"items": [{"metadata": {"name": "agent-abc-test", "resourceVersion": "42"},
            "spec": {"template": {"spec": {"containers": [{"name": "hermes-webui", "env": []}]}}}}]}
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "test", "new_password": "newsecret123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["subdomain"] == "test"
        password_secret = mock_k8s_post.call_args.args[1]
        assert password_secret["kind"] == "Secret"
        assert password_secret["stringData"]["password"] == "newsecret123"

    @patch("app.k8s_patch", side_effect=RuntimeError("patch failed"))
    @patch("app.k8s_get")
    @patch("app.k8s_post")
    def test_failed_password_rollout_is_not_reported_as_success(self, create, get, patch_deployment, client):
        get.return_value = {"items": [{"metadata": {"name": "agent-abc-test"},
            "spec": {"template": {"spec": {"containers": [{"name": "hermes-webui"}]}}}}]}
        response = client.post("/api/change-password", headers=AUTH_HEADERS,
                               json={"subdomain": "test", "new_password": "newsecret123"})
        assert response.status_code == 502

    def test_password_change_requires_authentication(self, client):
        response = client.post("/api/change-password", json={"subdomain": "test", "new_password": "newsecret123"})
        assert response.status_code == 401

    @patch("app.k8s_post")
    def test_change_password_rejects_empty_password(self, mock_k8s_post, client):
        """POST /api/change-password with empty password should return 400."""
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "test", "new_password": ""},
        )
        assert response.status_code == 400

    @patch("app.k8s_post")
    def test_change_password_rejects_short_password(self, mock_k8s_post, client):
        """POST /api/change-password with password shorter than 4 chars should return 400."""
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "test", "new_password": "abc"},
        )
        assert response.status_code == 400

    @patch("app.k8s_post")
    def test_change_password_rejects_invalid_subdomain(self, mock_k8s_post, client):
        """POST /api/change-password with invalid subdomain should return 400."""
        response = client.post(
            "/api/change-password",
            headers=AUTH_HEADERS,
            json={"subdomain": "INVALID", "new_password": "newsecret"},
        )
        assert response.status_code == 400


def test_admin_routes_require_token(client, monkeypatch):
    assert client.get('/api/instances').status_code == 401
    assert client.post('/api/web-extract', json={'urls': []}).status_code == 401
    assert client.get('/', auth=('admin', 'test-deploy-token')).status_code == 200
    assert client.post('/api/deploy', auth=('admin', 'test-deploy-token'),
                       json={'subdomain': 'x', **VALID_PASSWORD}).status_code == 403
    monkeypatch.setattr(app_module, 'DEPLOY_TOKEN', '')
    assert client.get('/api/instances', headers=AUTH_HEADERS).status_code == 401


def test_extractor_requires_proxy_and_bounds_batch(client, monkeypatch):
    monkeypatch.delenv('WEB_EXTRACT_PROXY', raising=False)
    assert client.post('/api/web-extract', headers=AUTH_HEADERS, json={'urls': []}).status_code == 503
    monkeypatch.setenv('WEB_EXTRACT_PROXY', 'http://proxy.example')
    for urls, status in [(['http://example.org'], 400), (['https://example.org'] * 9, 422)]:
        assert client.post('/api/web-extract', headers=AUTH_HEADERS, json={'urls': urls}).status_code == status


def test_extractor_bounds_downloads_and_ignores_redirects(monkeypatch):
    import httpx
    monkeypatch.setattr(app_module, 'MAX_PAGE_BYTES', 32)
    def handle(request):
        if request.url.path == '/redirect':
            return httpx.Response(302, headers={'location': '/ok'})
        data = b'x' * 33 if request.url.path == '/large' else b'public article content'
        return httpx.Response(200, stream=httpx.ByteStream(data))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        results = app_module.extract_pages(client, ['https://example.org/' + path for path in
                                           ('large', 'redirect', 'ok')], lambda data, **kw: data.decode())
    assert [bool(item['content']) for item in results['results']] == [False, False, True]


def test_duplicate_and_ambiguous_instances(client, monkeypatch):
    monkeypatch.setattr(app_module, 'k8s_get', lambda *a, **kw: {'items': [{}, {}]})
    with patch('app.k8s_post') as create:
        assert client.post('/api/deploy', headers=AUTH_HEADERS, json={'subdomain': 'used', **VALID_PASSWORD}).status_code == 409
        assert client.post('/api/change-password', headers=AUTH_HEADERS, json={'subdomain': 'used', 'new_password': 'newsecret'}).status_code == 409
        create.assert_not_called()


def test_isolated_credentials_and_selectors(client):
    import json
    keys = []
    for domain in ('tenant-a', 'tenant-b'):
        with patch('app.k8s_post') as create:
            assert client.post('/api/deploy', headers=AUTH_HEADERS, json={'subdomain': domain, **VALID_PASSWORD}).is_success
        objects = [c.args[1] for c in create.call_args_list]
        assert objects[0]['metadata']['name'] == 'agent-domain-' + domain
        keys.append(objects[1]['stringData']['API_SERVER_KEY'])
        for obj in objects:
            if obj['kind'] != 'Secret':
                assert 'test-llm-key' not in json.dumps(obj)
        dep = next(o for o in objects if o['kind'] == 'Deployment')
        svc = next(o for o in objects if o['kind'] == 'Service')
        assert dep['spec']['selector']['matchLabels'] == svc['spec']['selector']
        assert 'agent-hash' in svc['spec']['selector']
    assert keys[0] != keys[1]


class TestRequiredCreationPassword:
    @pytest.mark.parametrize('fields', [{}, {'password': '', 'password_confirmation': ''},
                                       {'password': None, 'password_confirmation': None}])
    @patch('app.k8s_post')
    def test_empty_or_omitted_password_is_rejected(self, create, client, fields):
        response = client.post('/api/deploy', headers=AUTH_HEADERS, json={'subdomain': 'nosw', **fields})
        assert response.status_code == 400
        create.assert_not_called()

    @pytest.mark.parametrize('password', ['instance-only-password', '  mot-dé-passe  '])
    @patch('app.k8s_post')
    def test_custom_password_only_changes_instance_secret(self, create, client, password):
        response = client.post('/api/deploy', headers=AUTH_HEADERS, json={
            'subdomain': 'custom', 'password': password, 'password_confirmation': password})
        assert response.status_code == 200
        secret = next(c.args[1] for c in create.call_args_list if c.args[0] == 'secrets')
        assert secret['stringData']['password'] == password
        deployment = next(c.args[1] for c in create.call_args_list if c.args[0] == 'deployments')
        webui = next(c for c in deployment['spec']['template']['spec']['containers'] if c['name'] == 'hermes-webui')
        password_env = next(e for e in webui['env'] if e['name'] == 'HERMES_WEBUI_PASSWORD')
        assert password_env['valueFrom']['secretKeyRef']['name'] == secret['metadata']['name']
        assert password not in response.text
        assert app_module.HERMES_WEBUI_PASSWORD == 'test-webui-password'

    @pytest.mark.parametrize('fields', [
        {},
        {'password': 'chosen-password', 'password_confirmation': 'different-password'},
        {'password': 'chosen-password'},
        {'password_confirmation': 'orphan-confirmation'},
        {'password': 'abc', 'password_confirmation': 'abc'},
    ])
    @patch('app.k8s_post')
    def test_invalid_password_rejected_before_kubernetes(self, create, client, fields):
        response = client.post('/api/deploy', headers=AUTH_HEADERS, json={'subdomain': 'invalid', **fields})
        assert response.status_code == 400
        create.assert_not_called()
        assert all(value not in response.text for value in fields.values())

    @patch('app.k8s_post')
    def test_custom_password_works_without_configured_default(self, create, client, monkeypatch):
        monkeypatch.setattr(app_module, 'HERMES_WEBUI_PASSWORD', '')
        response = client.post('/api/deploy', headers=AUTH_HEADERS, json={
            'subdomain': 'custom', 'password': 'chosen-password', 'password_confirmation': 'chosen-password'})
        assert response.status_code == 200

    def test_request_representation_masks_passwords(self):
        request = app_module.DeployRequest(subdomain='custom', password='chosen-password',
                                           password_confirmation='chosen-password')
        assert 'chosen-password' not in repr(request)


def test_reservation_conflict_stops_provisioning(client, monkeypatch):
    from urllib.error import HTTPError
    monkeypatch.setattr(app_module, "get_token", lambda: "fake-k8s-token")
    conflict = HTTPError("https://unused", 409, "AlreadyExists", {}, None)
    with patch("urllib.request.urlopen", side_effect=conflict) as send:
        response = client.post("/api/deploy", headers=AUTH_HEADERS,
                               json={"subdomain": "contended", **VALID_PASSWORD})
    assert response.status_code == 409
    send.assert_called_once()
    assert send.call_args.args[0].full_url.endswith("/configmaps")


@pytest.mark.parametrize("expose, ingress_count", [("false", 1), ("true", 2)])
def test_browser_can_provision_and_gateway_is_optional(client, monkeypatch, expose, ingress_count):
    monkeypatch.setenv("EXPOSE_GATEWAY_INGRESS", expose)
    with patch("app.k8s_post") as create:
        response = client.post("/api/deploy", auth=("admin", "test-deploy-token"),
                               headers={"X-Dashboard-Request": "1"},
                               json={"subdomain": "web", **VALID_PASSWORD})
    assert response.is_success
    assert sum(call.args[0] == "ingresses" for call in create.call_args_list) == ingress_count


def test_startup_rejects_empty_token(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(app_module, "DEPLOY_TOKEN", " ")
    with pytest.raises(RuntimeError, match="DEPLOY_TOKEN"):
        with TestClient(app):
            pass
