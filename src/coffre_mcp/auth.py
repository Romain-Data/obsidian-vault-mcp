"""Mini serveur d'autorisation OAuth 2.1, pour un seul utilisateur et un seul client (claude.ai).

Rôles (spécification MCP, autorisation) :
- le serveur MCP est un « resource server » : il vérifie le jeton à chaque requête (TokenVerifierSQLite) ;
- ce module est l'« authorization server » : il fait se connecter le propriétaire du coffre et délivre les jetons.
Les deux vivent dans le même service, à la même adresse publique.

Parcours (code d'autorisation + PKCE) :
1. claude.ai découvre tout seul ce module via /.well-known/oauth-protected-resource/mcp puis
   /.well-known/oauth-authorization-server ;
2. il ouvre /authorize dans le navigateur : le propriétaire saisit sa phrase de passe + le code à 6 chiffres
   de son appli d'authentification (TOTP) ;
3. le navigateur revient sur claude.ai avec un code à usage unique (2 min) ;
4. claude.ai l'échange sur /token contre un jeton d'accès (1 h) et un jeton de renouvellement (30 j, changé
   à chaque usage). Seule l'empreinte SHA-256 des jetons est stockée : une fuite de la base ne donne rien.

Client pré-enregistré (choix « Use your own OAuth client » dans claude.ai) : identifiant + secret dans le .env.
Pas d'enregistrement dynamique : personne d'autre ne peut se déclarer comme client.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlencode, urlparse

import pyotp
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from mcp.server.auth.provider import AccessToken

log = logging.getLogger("coffre_mcp.auth")

DUREE_CODE = 120
DUREE_ACCES = 3600
DUREE_RENOUVELLEMENT = 30 * 24 * 3600
DUREE_DEMANDE = 600
ECHECS_MAX = 5
FENETRE_ECHECS = 15 * 60
SCOPE = "coffre"


# ---------------------------------------------------------------- phrase de passe
def hacher_phrase(phrase: str, *, n: int = 2**15, r: int = 8, p: int = 1) -> str:
    sel = secrets.token_bytes(16)
    h = hashlib.scrypt(phrase.encode(), salt=sel, n=n, r=r, p=p, maxmem=128 * 1024 * 1024, dklen=32)
    b = base64.b64encode
    return f"scrypt:{n}:{r}:{p}:{b(sel).decode()}:{b(h).decode()}"


def verifier_phrase(phrase: str, empreinte: str) -> bool:
    try:
        algo, n, r, p, sel, h = empreinte.split(":")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(phrase.encode(), salt=base64.b64decode(sel), n=int(n), r=int(r), p=int(p),
                              maxmem=128 * 1024 * 1024, dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(h))
    except Exception:
        return False


def _empreinte(jeton: str) -> str:
    return hashlib.sha256(jeton.encode()).hexdigest()


def _s256(verifieur: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifieur.encode()).digest()).rstrip(b"=").decode()


# ---------------------------------------------------------------- réglages
@dataclass
class ReglagesAuth:
    url_publique: str                    # https://mcp.example.com (= issuer)
    client_id: str
    client_secret: str
    redirect_uris: list[str]             # exactement https://claude.ai/api/mcp/auth_callback
    empreinte_phrase: str
    totp_secret: str
    fichier_base: Path
    chemin_mcp: str = "/mcp"

    @property
    def issuer(self) -> str:
        return self.url_publique.rstrip("/")

    @property
    def ressource(self) -> str:
        return self.issuer + self.chemin_mcp


# ---------------------------------------------------------------- stockage
class Stockage:
    def __init__(self, fichier: Path):
        fichier.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(fichier, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS codes (empreinte TEXT PRIMARY KEY, client_id TEXT, redirect_uri TEXT,
                challenge TEXT, ressource TEXT, scope TEXT, expire REAL, utilise INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS jetons (empreinte TEXT PRIMARY KEY, type TEXT, famille TEXT,
                client_id TEXT, ressource TEXT, scope TEXT, expire REAL, revoque INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS etat (cle TEXT PRIMARY KEY, valeur TEXT);
        """)
        self._verrou = asyncio.Lock()

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        return self._db.execute(sql, args)

    def nettoyer(self) -> None:
        t = time.time()
        self.execute("DELETE FROM codes WHERE expire < ?", (t - 3600,))
        self.execute("DELETE FROM jetons WHERE expire < ?", (t - 86400,))


# ---------------------------------------------------------------- vérification des jetons (resource server)
class TokenVerifierSQLite:
    def __init__(self, stockage: Stockage, reglages: ReglagesAuth):
        self.s, self.r = stockage, reglages

    async def verify_token(self, token: str) -> AccessToken | None:
        ligne = self.s.execute(
            "SELECT client_id, ressource, scope, expire, revoque FROM jetons WHERE empreinte=? AND type='acces'",
            (_empreinte(token),),
        ).fetchone()
        if not ligne:
            return None
        client_id, ressource, scope, expire, revoque = ligne
        if revoque or expire < time.time() or ressource != self.r.ressource:
            return None
        return AccessToken(token=token, client_id=client_id, scopes=scope.split(), expires_at=int(expire),
                           resource=ressource)


# ---------------------------------------------------------------- serveur d'autorisation
@dataclass
class _Demande:
    client_id: str
    redirect_uri: str
    state: str | None
    challenge: str
    scope: str
    ressource: str
    expire: float


@dataclass
class ServeurAutorisation:
    r: ReglagesAuth
    s: Stockage
    _demandes: dict[str, _Demande] = field(default_factory=dict)
    _echecs: list[float] = field(default_factory=list)

    # ---- métadonnées (RFC 8414)
    async def metadonnees(self, request: Request) -> Response:
        i = self.r.issuer
        return JSONResponse({
            "issuer": i,
            "authorization_endpoint": f"{i}/authorize",
            "token_endpoint": f"{i}/token",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"],
            "scopes_supported": [SCOPE],
            "authorization_response_iss_parameter_supported": True,
        }, headers={"Cache-Control": "public, max-age=3600"})

    # ---- /authorize
    def _page(self, titre: str, corps: str, statut: int = 200, action_vers: str | None = None) -> HTMLResponse:
        form_action = "'self'" + (f" {action_vers}" if action_vers else "")
        page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(titre)}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:26rem;margin:3rem auto;padding:0 1rem;color:#222}}
input{{display:block;width:100%;padding:.6rem;margin:.3rem 0 1rem;font-size:1rem;box-sizing:border-box}}
button{{padding:.7rem 1.2rem;font-size:1rem}} .err{{color:#b00020}} small{{color:#666}}</style></head>
<body><h1>{html.escape(titre)}</h1>{corps}</body></html>"""
        return HTMLResponse(page, status_code=statut, headers={
            "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": f"default-src 'none'; style-src 'unsafe-inline'; form-action {form_action}; "
                                       "frame-ancestors 'none'; base-uri 'none'",
        })

    def _erreur_vers_client(self, d: _Demande, erreur: str, description: str) -> Response:
        q = {"error": erreur, "error_description": description, "iss": self.r.issuer}
        if d.state is not None:
            q["state"] = d.state
        return RedirectResponse(f"{d.redirect_uri}?{urlencode(q)}", status_code=302)

    async def autoriser(self, request: Request) -> Response:
        if request.method == "POST":
            return await self._autoriser_post(request)
        p = request.query_params
        client_id, redirect_uri = p.get("client_id", ""), p.get("redirect_uri", "")
        # Client ou adresse de retour inconnus : on n'y redirige JAMAIS (sinon redirection ouverte)
        if not hmac.compare_digest(client_id, self.r.client_id) or redirect_uri not in self.r.redirect_uris:
            return self._page("Demande refusée", "<p class=err>Application inconnue.</p>", 400)
        d = _Demande(client_id, redirect_uri, p.get("state"), p.get("code_challenge", ""), p.get("scope") or SCOPE,
                     p.get("resource") or self.r.ressource, time.time() + DUREE_DEMANDE)
        if p.get("response_type") != "code":
            return self._erreur_vers_client(d, "unsupported_response_type", "response_type=code attendu")
        if p.get("code_challenge_method") != "S256" or len(d.challenge) < 43:
            return self._erreur_vers_client(d, "invalid_request", "PKCE S256 obligatoire")
        if d.ressource.rstrip("/") != self.r.ressource:
            return self._erreur_vers_client(d, "invalid_target", "ressource inconnue")
        if set(d.scope.split()) - {SCOPE}:
            return self._erreur_vers_client(d, "invalid_scope", f"seul le scope « {SCOPE} » existe")
        self._purger_demandes()
        ident = secrets.token_urlsafe(24)
        self._demandes[ident] = d
        return self._formulaire(ident, d)

    def _formulaire(self, ident: str, d: _Demande, erreur: str = "", statut: int = 200) -> HTMLResponse:
        hote = urlparse(d.redirect_uri)
        err = f"<p class=err>{html.escape(erreur)}</p>" if erreur else ""
        corps = f"""<p><strong>{html.escape(hote.hostname or '')}</strong> demande l'accès à ton coffre Obsidian
(lecture, recherche, écriture ; jamais de suppression).</p>{err}
<form method="post" action="/authorize" autocomplete="off">
<input type="hidden" name="demande" value="{html.escape(ident)}">
<label>Phrase de passe<input type="password" name="phrase" required autofocus autocomplete="current-password"></label>
<label>Code à 6 chiffres (appli d'authentification)<input name="code" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autocomplete="one-time-code"></label>
<button type="submit">Autoriser</button></form>
<p><small>Tu n'es pas à l'origine de cette demande ? Ferme simplement cette page.</small></p>"""
        origine = f"{hote.scheme}://{hote.netloc}"
        return self._page("Accès au coffre", corps, statut, action_vers=origine)

    def _purger_demandes(self) -> None:
        t = time.time()
        for k in [k for k, v in self._demandes.items() if v.expire < t]:
            del self._demandes[k]

    def _verrouille(self) -> bool:
        t = time.time()
        self._echecs = [x for x in self._echecs if x > t - FENETRE_ECHECS]
        return len(self._echecs) >= ECHECS_MAX

    def _verifier_totp(self, code: str) -> bool:
        totp = pyotp.TOTP(self.r.totp_secret)
        maintenant = time.time()
        dernier = self.s.execute("SELECT valeur FROM etat WHERE cle='totp_dernier_pas'").fetchone()
        dernier_pas = int(dernier[0]) if dernier else -1
        for decalage in (-1, 0, 1):
            t = maintenant + decalage * totp.interval
            pas = int(t // totp.interval)
            if hmac.compare_digest(totp.at(t), code) and pas > dernier_pas:  # anti-rejeu : un code = une fois
                self.s.execute("INSERT OR REPLACE INTO etat VALUES ('totp_dernier_pas', ?)", (str(pas),))
                return True
        return False

    async def _autoriser_post(self, request: Request) -> Response:
        f = await request.form()
        ident = str(f.get("demande", ""))
        self._purger_demandes()
        d = self._demandes.get(ident)
        if d is None:
            return self._page("Demande expirée", "<p class=err>Cette demande a expiré. Relance la connexion depuis "
                              "claude.ai.</p>", 400)
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
        if self._verrouille():
            log.warning("connexion refusée (verrouillage) depuis %s", ip)
            return self._formulaire(ident, d, "Trop d'essais : réessaie dans 15 minutes.", 429)
        phrase_ok = await asyncio.to_thread(verifier_phrase, str(f.get("phrase", "")), self.r.empreinte_phrase)
        code_ok = self._verifier_totp(str(f.get("code", "")).strip())
        if not (phrase_ok and code_ok):
            self._echecs.append(time.time())
            log.warning("échec de connexion depuis %s", ip)
            await asyncio.sleep(1)
            return self._formulaire(ident, d, "Phrase de passe ou code incorrect.", 401)
        del self._demandes[ident]
        self._echecs.clear()
        code = secrets.token_urlsafe(32)
        self.s.execute("INSERT INTO codes VALUES (?,?,?,?,?,?,?,0)", (_empreinte(code), d.client_id, d.redirect_uri,
                       d.challenge, self.r.ressource, SCOPE, time.time() + DUREE_CODE))
        log.info("connexion réussie depuis %s", ip)
        q = {"code": code, "iss": self.r.issuer}
        if d.state is not None:
            q["state"] = d.state
        return RedirectResponse(f"{d.redirect_uri}?{urlencode(q)}", status_code=302,
                                headers={"Cache-Control": "no-store"})

    # ---- /token
    @staticmethod
    def _erreur_jeton(erreur: str, description: str, statut: int = 400) -> JSONResponse:
        h = {"Cache-Control": "no-store"}
        if erreur == "invalid_client":
            h["WWW-Authenticate"] = 'Basic realm="token"'
        return JSONResponse({"error": erreur, "error_description": description}, status_code=statut, headers=h)

    def _client_authentifie(self, request: Request, f) -> bool:
        cid, secret = f.get("client_id"), f.get("client_secret")
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            try:
                brut = base64.b64decode(auth[6:]).decode()
                a, _, b = brut.partition(":")
                cid, secret = unquote(a), unquote(b)
            except Exception:
                return False
        return bool(cid and secret) and hmac.compare_digest(str(cid), self.r.client_id) and \
            hmac.compare_digest(str(secret), self.r.client_secret)

    def _emettre(self, famille: str, client_id: str) -> JSONResponse:
        acces, renouv = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        t = time.time()
        self.s.execute("INSERT INTO jetons VALUES (?,?,?,?,?,?,?,0)", (_empreinte(acces), "acces", famille, client_id,
                       self.r.ressource, SCOPE, t + DUREE_ACCES))
        self.s.execute("INSERT INTO jetons VALUES (?,?,?,?,?,?,?,0)", (_empreinte(renouv), "renouvellement", famille,
                       client_id, self.r.ressource, SCOPE, t + DUREE_RENOUVELLEMENT))
        self.s.nettoyer()
        return JSONResponse({"access_token": acces, "token_type": "Bearer", "expires_in": DUREE_ACCES,
                             "refresh_token": renouv, "scope": SCOPE},
                            headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def jeton(self, request: Request) -> Response:
        if "application/x-www-form-urlencoded" not in request.headers.get("content-type", ""):
            return self._erreur_jeton("invalid_request", "corps attendu en application/x-www-form-urlencoded")
        f = await request.form()
        if not self._client_authentifie(request, f):
            return self._erreur_jeton("invalid_client", "client inconnu ou secret incorrect", 401)
        ressource = f.get("resource")
        if ressource and str(ressource).rstrip("/") != self.r.ressource:
            return self._erreur_jeton("invalid_target", "ressource inconnue")
        async with self.s._verrou:
            if f.get("grant_type") == "authorization_code":
                return self._echanger_code(f)
            if f.get("grant_type") == "refresh_token":
                return self._renouveler(f)
        return self._erreur_jeton("unsupported_grant_type", "authorization_code ou refresh_token")

    def _echanger_code(self, f) -> Response:
        e = _empreinte(str(f.get("code", "")))
        ligne = self.s.execute("SELECT client_id, redirect_uri, challenge, expire, utilise FROM codes "
                               "WHERE empreinte=?", (e,)).fetchone()
        if not ligne:
            return self._erreur_jeton("invalid_grant", "code inconnu")
        client_id, redirect_uri, challenge, expire, utilise = ligne
        self.s.execute("UPDATE codes SET utilise=1 WHERE empreinte=?", (e,))
        if utilise:
            # Code rejoué : on révoque ce qu'il avait produit (RFC 6749 §4.1.2)
            self.s.execute("UPDATE jetons SET revoque=1 WHERE famille=?", (e,))
            return self._erreur_jeton("invalid_grant", "code déjà utilisé")
        if expire < time.time():
            return self._erreur_jeton("invalid_grant", "code expiré")
        if f.get("redirect_uri") != redirect_uri:
            return self._erreur_jeton("invalid_grant", "redirect_uri différente")
        verifieur = str(f.get("code_verifier", ""))
        if not (43 <= len(verifieur) <= 128) or not hmac.compare_digest(_s256(verifieur), challenge):
            return self._erreur_jeton("invalid_grant", "code_verifier incorrect (PKCE)")
        return self._emettre(famille=e, client_id=client_id)

    def _renouveler(self, f) -> Response:
        e = _empreinte(str(f.get("refresh_token", "")))
        ligne = self.s.execute("SELECT famille, client_id, expire, revoque FROM jetons "
                               "WHERE empreinte=? AND type='renouvellement'", (e,)).fetchone()
        if not ligne:
            return self._erreur_jeton("invalid_grant", "jeton inconnu")
        famille, client_id, expire, revoque = ligne
        if revoque:
            # Jeton de renouvellement déjà utilisé : signe de vol → toute la famille est révoquée
            self.s.execute("UPDATE jetons SET revoque=1 WHERE famille=?", (famille,))
            log.warning("réutilisation d'un jeton de renouvellement : famille révoquée")
            return self._erreur_jeton("invalid_grant", "jeton déjà utilisé")
        if expire < time.time():
            return self._erreur_jeton("invalid_grant", "jeton expiré")
        self.s.execute("UPDATE jetons SET revoque=1 WHERE empreinte=?", (e,))  # rotation
        return self._emettre(famille=famille, client_id=client_id)
