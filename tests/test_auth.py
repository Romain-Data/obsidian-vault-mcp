"""Parcours OAuth complet, comme le ferait claude.ai, puis appel d'un outil MCP avec le jeton obtenu."""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from starlette.testclient import TestClient

from coffre_mcp.auth import ReglagesAuth, hacher_phrase
from coffre_mcp.coffre import Coffre
from coffre_mcp.server import construire
from mcp.server.transport_security import TransportSecuritySettings

from test_coffre import FauxNextcloud

BASE = "https://mcp.test"
CB = "https://claude.ai/api/mcp/auth_callback"
PHRASE = "cheval correct pile agrafe"
TOTP = pyotp.random_base32()


@pytest.fixture
def client(tmp_path):
    (tmp_path / "coffre").mkdir()
    (tmp_path / "coffre" / "_index.md").write_text("Index\n")
    reglages = ReglagesAuth(url_publique=BASE, client_id="claude", client_secret="s3cret", redirect_uris=[CB],
                            empreinte_phrase=hacher_phrase(PHRASE, n=2**10), totp_secret=TOTP,
                            fichier_base=tmp_path / "oauth.sqlite3")
    coffre = Coffre(tmp_path / "coffre", FauxNextcloud(tmp_path / "coffre"))
    mcp, auth = construire(coffre, reglages)
    app = mcp.streamable_http_app(json_response=True, stateless_http=True, transport_security=TransportSecuritySettings(
        allowed_hosts=["mcp.test"], allowed_origins=["https://claude.ai"]))
    with TestClient(app, base_url=BASE) as c:
        c.auth_serveur = auth
        yield c


def pkce():
    v = secrets.token_urlsafe(48)
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def params(challenge, **extra):
    p = dict(response_type="code", client_id="claude", redirect_uri=CB, code_challenge=challenge,
             code_challenge_method="S256", state="xyz", scope="coffre", resource=f"{BASE}/mcp")
    p.update(extra)
    return p


def connexion(client, challenge, phrase=PHRASE, code=None):
    r = client.get("/authorize", params=params(challenge))
    assert r.status_code == 200
    ident = r.text.split('name="demande" value="')[1].split('"')[0]
    code = code or pyotp.TOTP(TOTP).now()
    return client.post("/authorize", data={"demande": ident, "phrase": phrase, "code": code}, follow_redirects=False)


def obtenir_jetons(client):
    v, ch = pkce()
    r = connexion(client, ch)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"] and q["iss"] == [BASE]
    r = client.post("/token", data=dict(grant_type="authorization_code", code=q["code"][0], redirect_uri=CB,
                                        code_verifier=v, client_id="claude", client_secret="s3cret",
                                        resource=f"{BASE}/mcp"))
    assert r.status_code == 200, r.text
    return r.json()


def test_decouverte(client):
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    assert 'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource/mcp"' in r.headers["www-authenticate"]
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"] == f"{BASE}/mcp" and prm["authorization_servers"][0].rstrip("/") == BASE
    asm = client.get("/.well-known/oauth-authorization-server").json()
    assert asm["code_challenge_methods_supported"] == ["S256"] and asm["issuer"] == BASE


def test_client_ou_redirection_inconnus_ne_redirigent_jamais(client):
    _, ch = pkce()
    assert client.get("/authorize", params=params(ch, client_id="pirate"), follow_redirects=False).status_code == 400
    r = client.get("/authorize", params=params(ch, redirect_uri="https://evil.example/cb"), follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers


def test_pkce_obligatoire(client):
    r = client.get("/authorize", params=params("x" * 43, code_challenge_method="plain"), follow_redirects=False)
    assert r.status_code == 302 and "error=invalid_request" in r.headers["location"]


def test_mauvaise_phrase_puis_verrouillage(client):
    _, ch = pkce()
    for _ in range(5):
        assert connexion(client, ch, phrase="faux").status_code == 401
    assert connexion(client, ch).status_code == 429  # même la bonne phrase est refusée pendant 15 min


def test_code_totp_non_rejouable(client):
    code = pyotp.TOTP(TOTP).now()
    _, ch = pkce()
    assert connexion(client, ch, code=code).status_code == 302
    assert connexion(client, ch, code=code).status_code == 401


def test_flux_complet_et_appel_d_outil(client):
    j = obtenir_jetons(client)
    h = {"Authorization": f"Bearer {j['access_token']}", "Accept": "application/json, text/event-stream",
         "MCP-Protocol-Version": "2025-11-25"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    assert client.post("/mcp", json=init, headers=h).status_code == 200
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, headers=h)
    noms = {t["name"] for t in r.json()["result"]["tools"]}
    assert noms == {"lister", "lire_note", "chercher", "ecrire_note", "ajouter_a_note", "remplacer_dans_note"}
    # Chaque outil doit se présenter seul : certains clients n'affichent ni les instructions ni le nom du serveur
    for t in r.json()["result"]["tools"]:
        assert "coffre Obsidian personnel" in t["description"], t["name"]
        assert len(t["description"]) < 600, t["name"]
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "lire_note", "arguments": {"chemin": "_index.md"}}}, headers=h)
    assert "Index" in r.json()["result"]["content"][0]["text"]
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "lire_note", "arguments": {"chemin": "../etc/passwd"}}}, headers=h)
    assert r.json()["result"]["isError"] is True


def test_code_a_usage_unique_et_mauvais_verifieur(client):
    v, ch = pkce()
    q = parse_qs(urlparse(connexion(client, ch).headers["location"]).query)
    base = dict(grant_type="authorization_code", code=q["code"][0], redirect_uri=CB, client_id="claude",
                client_secret="s3cret")
    assert client.post("/token", data={**base, "code_verifier": "a" * 50}).json()["error"] == "invalid_grant"
    assert client.post("/token", data={**base, "code_verifier": v}).json()["error"] == "invalid_grant"  # déjà consommé


def test_mauvais_secret_client(client):
    r = client.post("/token", data=dict(grant_type="refresh_token", refresh_token="x", client_id="claude",
                                        client_secret="faux"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_client"


def test_renouvellement_avec_rotation_et_detection_de_vol(client):
    j = obtenir_jetons(client)
    data = dict(grant_type="refresh_token", client_id="claude", client_secret="s3cret")
    j2 = client.post("/token", data={**data, "refresh_token": j["refresh_token"]}).json()
    assert j2["refresh_token"] != j["refresh_token"]
    # Réutiliser l'ancien jeton : refus ET révocation de toute la famille
    assert client.post("/token", data={**data, "refresh_token": j["refresh_token"]}).json()["error"] == "invalid_grant"
    assert client.post("/token", data={**data, "refresh_token": j2["refresh_token"]}).json()["error"] == "invalid_grant"
    h = {"Authorization": f"Bearer {j2['access_token']}", "Accept": "application/json, text/event-stream"}
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h).status_code == 401


def test_origine_etrangere_refusee(client):
    j = obtenir_jetons(client)
    h = {"Authorization": f"Bearer {j['access_token']}", "Origin": "https://evil.example",
         "Accept": "application/json, text/event-stream"}
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=h).status_code == 403


def test_description_du_coffre_personnalisable(tmp_path):
    """COFFRE_DESCRIPTION : chacun décrit son coffre sans toucher au code (ni publier ce qu'il contient)."""
    (tmp_path / "c").mkdir()
    reglages = ReglagesAuth(url_publique=BASE, client_id="claude", client_secret="s", redirect_uris=[CB],
                            empreinte_phrase=hacher_phrase(PHRASE, n=2**10), totp_secret=TOTP,
                            fichier_base=tmp_path / "oauth.sqlite3")
    mcp, _ = construire(Coffre(tmp_path / "c", FauxNextcloud(tmp_path / "c")), reglages,
                        description="coffre de test : recettes de cuisine", titre="Recettes")
    assert "recettes de cuisine" in mcp.instructions
    for outil in mcp._tool_manager.list_tools():
        assert "coffre de test : recettes de cuisine" in outil.description
