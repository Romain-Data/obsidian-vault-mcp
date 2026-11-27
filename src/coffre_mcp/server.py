"""Serveur MCP « coffre » : donne à Claude un accès contrôlé au coffre Obsidian (un dossier Nextcloud).

Outils : lister, lire_note, chercher, ecrire_note, ajouter_a_note, remplacer_dans_note ;
et, si AGENDA_ACTIF=1 : lister_agendas, lister_evenements, creer_evenement, modifier_evenement (CalDAV).
Aucun outil de suppression ; .obsidian/ et les fichiers cachés sont inaccessibles.
Lancement : `uvicorn coffre_mcp.server:creer_app --factory --host 0.0.0.0 --port 8000`
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .agenda import ClientCalDAV
from .auth import SCOPE, ReglagesAuth, ServeurAutorisation, Stockage, TokenVerifierSQLite
from .coffre import Coffre, ErreurCoffre, Resultat
from .webdav import ClientWebDAV

# Ce que contient le coffre, répété dans chaque description d'outil : certains clients n'affichent ni les
# instructions ni le nom du serveur. Personnalisable sans toucher au code (variable COFFRE_DESCRIPTION).
COFFRE_DEFAUT = "coffre Obsidian personnel de l'utilisateur (notes Markdown : projets, journal, références)"
CARTE = "Point d'entrée : « _index.md » (une ligne par note) ; lis-le d'abord pour savoir quelle note ouvrir."

INSTRUCTIONS = """Accès au {coffre}.
Commence par lire « _index.md » (le point d'entrée du coffre) et « _Conventions.md » avant d'écrire.
Les chemins sont relatifs au coffre, par exemple « Projets/Mon projet.md ».
Pour modifier une note existante, préfère ajouter_a_note ou remplacer_dans_note à une réécriture complète.
Il n'existe pas de suppression : si une note doit disparaître, signale-le à l'utilisateur."""

INSTRUCTIONS_AGENDA = """
Agenda : lister_evenements pour consulter, creer_evenement / modifier_evenement pour écrire. Les heures sont
dans le fuseau de l'utilisateur ({fuseau}). Pas de suppression d'événement : si l'utilisateur veut en supprimer
un, dis-lui de le faire dans son appli d'agenda."""

LECTURE = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
ECRITURE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)


def _env(nom: str, defaut: str | None = None) -> str:
    v = os.environ.get(nom, defaut)
    if v is None or v == "":
        raise SystemExit(f"Variable d'environnement manquante : {nom}")
    return v


def construire(coffre: Coffre, reglages: ReglagesAuth, *, description: str = COFFRE_DEFAUT,
               titre: str = "Coffre Obsidian",
               agenda: ClientCalDAV | None = None) -> tuple[MCPServer, ServeurAutorisation]:
    COFFRE = description
    stockage = Stockage(reglages.fichier_base)
    serveur_auth = ServeurAutorisation(reglages, stockage)
    mcp = MCPServer(
        name="coffre-obsidian",
        title=titre,
        instructions=INSTRUCTIONS.format(coffre=description)
        + (INSTRUCTIONS_AGENDA.format(fuseau=agenda.tz.key) if agenda else ""),
        token_verifier=TokenVerifierSQLite(stockage, reglages),
        auth=AuthSettings(
            issuer_url=reglages.issuer,
            resource_server_url=reglages.ressource,
            required_scopes=[SCOPE],
            validate_token_resource=True,
        ),
    )

    def _ok(f):
        try:
            return f()
        except ErreurCoffre as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=LECTURE, description=(
        f"Liste les fichiers d'un dossier du {COFFRE}. Racine par défaut ; les dossiers finissent par « / » ; "
        f"recursif=true descend dans les sous-dossiers. {CARTE}"))
    def lister(dossier: str = "", recursif: bool = False) -> str:
        elements = _ok(lambda: coffre.lister(dossier, recursif))
        return "\n".join(elements) or "(dossier vide)"

    @mcp.tool(annotations=LECTURE, description=(
        f"Lit une note du {COFFRE}. À utiliser dès que la demande porte sur un sujet que le coffre peut couvrir. "
        f"Chemin relatif, ex. « Projets/Mon projet.md ». {CARTE}"))
    def lire_note(chemin: str) -> str:
        return _ok(lambda: coffre.lire(chemin))

    @mcp.tool(annotations=LECTURE, description=(
        f"Recherche plein texte (insensible à la casse, sans regex) dans les notes du {COFFRE}, ou dans un "
        f"sous-dossier. Renvoie « chemin:ligne: extrait », 5 résultats max par note."))
    async def chercher(requete: str, dossier: str = "", max_resultats: int = 50) -> str:
        try:
            res: list[Resultat] = await coffre.chercher(requete, dossier, max_resultats)
        except ErreurCoffre as e:
            raise ToolError(str(e)) from None
        return "\n".join(f"{r.chemin}:{r.ligne}: {r.texte}" for r in res) or "Aucun résultat."

    @mcp.tool(annotations=ECRITURE, description=(
        f"Crée une note Markdown (.md) dans le {COFFRE} (dossiers créés si besoin). Lis « _Conventions.md » avant. "
        f"Refuse si la note existe, sauf remplacer_si_existe=true (réécriture complète ; Nextcloud garde l'ancienne "
        f"version)."))
    async def ecrire_note(chemin: str, contenu: str, remplacer_si_existe: bool = False) -> str:
        try:
            return await coffre.ecrire_note(chemin, contenu, remplacer_si_existe)
        except ErreurCoffre as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=ECRITURE, description=(
        f"Ajoute du texte à la fin d'une note existante du {COFFRE} (saut de ligne inséré si besoin)."))
    async def ajouter_a_note(chemin: str, texte: str) -> str:
        try:
            return await coffre.ajouter_a_note(chemin, texte)
        except ErreurCoffre as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=ECRITURE, description=(
        f"Remplace un passage d'une note du {COFFRE}. « ancien » doit apparaître exactement une fois (copie-le "
        f"tel quel, avec assez de contexte pour être unique)."))
    async def remplacer_dans_note(chemin: str, ancien: str, nouveau: str) -> str:
        try:
            return await coffre.remplacer_dans_note(chemin, ancien, nouveau)
        except ErreurCoffre as e:
            raise ToolError(str(e)) from None

    if agenda is not None:
        _outils_agenda(mcp, agenda)

    # Serveur d'autorisation, servi à la même adresse
    mcp.custom_route("/.well-known/oauth-authorization-server", ["GET"])(serveur_auth.metadonnees)
    mcp.custom_route("/authorize", ["GET", "POST"])(serveur_auth.autoriser)
    mcp.custom_route("/token", ["POST"])(serveur_auth.jeton)

    @mcp.custom_route("/sante", ["GET"])
    async def sante(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return mcp, serveur_auth


def _outils_agenda(mcp: MCPServer, agenda: ClientCalDAV) -> None:
    tz = agenda.tz.key
    AGENDA = "agenda personnel de l'utilisateur (Nextcloud, synchronisé sur son téléphone)"
    FORMATS = (f"Dates au format 2026-11-29 (journée) ou 2026-11-29T10:00 (heure locale, fuseau {tz}).")

    async def _ok(coro):
        try:
            return await coro
        except ErreurCoffre as e:
            raise ToolError(str(e)) from None

    @mcp.tool(annotations=LECTURE, description=(
        f"Liste les agendas visibles de l'{AGENDA}, avec leur id, leurs droits et celui utilisé par défaut."))
    async def lister_agendas() -> str:
        return await _ok(agenda.lister_agendas())

    @mcp.tool(annotations=LECTURE, description=(
        f"Liste les événements de l'{AGENDA} entre « debut » et « fin » (fin incluse ; sans fin : la journée de "
        f"debut), récurrences développées. Tous les agendas sauf si « agenda_nom » est précisé (nom ou id). {FORMATS} "
        f"Chaque ligne donne l'id à passer à modifier_evenement."))
    async def lister_evenements(debut: str, fin: str = "", agenda_nom: str = "") -> str:
        return await _ok(agenda.lister(debut, fin or None, agenda_nom or None))

    @mcp.tool(annotations=ECRITURE, description=(
        f"Crée un événement dans l'{AGENDA}. {FORMATS} Sans fin : 1 h (ou la journée si debut est une date). "
        f"Journée entière : journee_entiere=true, fin = dernier jour inclus. « agenda_nom » : nom ou id (sinon "
        f"l'agenda par défaut). rappel_minutes : alerte N minutes avant. recurrence : règle RRULE, ex. "
        f"« FREQ=WEEKLY;BYDAY=SU » (chaque dimanche) ou « FREQ=WEEKLY;BYDAY=SU;COUNT=10 »."))
    async def creer_evenement(titre: str, debut: str, fin: str = "", journee_entiere: bool = False,
                              lieu: str = "", description: str = "", rappel_minutes: int | None = None,
                              recurrence: str = "", agenda_nom: str = "") -> str:
        return await _ok(agenda.creer(titre, debut, fin or None, agenda=agenda_nom or None,
                                      journee_entiere=journee_entiere, lieu=lieu, description=description,
                                      rappel_minutes=rappel_minutes, recurrence=recurrence))

    @mcp.tool(annotations=ECRITURE, description=(
        f"Modifie un événement de l'{AGENDA} (id donné par lister_evenements). Seuls les champs fournis changent ; "
        f"chaîne vide pour effacer lieu, description ou récurrence ; rappel_minutes=-1 retire le rappel. Changer "
        f"seulement le début garde la durée. Sur un événement récurrent, modifie TOUTE la série. {FORMATS}"))
    async def modifier_evenement(id_evenement: str, titre: str | None = None, debut: str | None = None,
                                 fin: str | None = None, journee_entiere: bool | None = None,
                                 lieu: str | None = None, description: str | None = None,
                                 rappel_minutes: int | None = None, recurrence: str | None = None) -> str:
        return await _ok(agenda.modifier(id_evenement, titre=titre, debut=debut, fin=fin,
                                         journee_entiere=journee_entiere, lieu=lieu, description=description,
                                         rappel_minutes=rappel_minutes, recurrence=recurrence))


def creer_app() -> Starlette:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    url_publique = _env("PUBLIC_URL").rstrip("/")
    reglages = ReglagesAuth(
        url_publique=url_publique,
        client_id=_env("OAUTH_CLIENT_ID"),
        client_secret=_env("OAUTH_CLIENT_SECRET"),
        redirect_uris=[u.strip() for u in _env("OAUTH_REDIRECT_URIS",
                                               "https://claude.ai/api/mcp/auth_callback").split(",") if u.strip()],
        empreinte_phrase=_env("LOGIN_PASSPHRASE_HASH"),
        totp_secret=_env("TOTP_SECRET"),
        fichier_base=Path(_env("DATA_DIR", "/data")) / "oauth.sqlite3",
    )
    ecrivain = ClientWebDAV(
        base_url=_env("NEXTCLOUD_URL", "http://nextcloud-aio-apache:11000"),
        utilisateur=_env("NEXTCLOUD_USER", "claude-mcp"),
        mot_de_passe=_env("NEXTCLOUD_APP_PASSWORD"),
        dossier=_env("NEXTCLOUD_DOSSIER", "Obsidian"),
        hote=os.environ.get("NEXTCLOUD_HOST") or None,
    )
    coffre = Coffre(Path(_env("COFFRE_DIR", "/coffre")), ecrivain)
    agenda = None
    if os.environ.get("AGENDA_ACTIF", "").strip().lower() in ("1", "true", "oui", "yes"):
        agenda = ClientCalDAV(
            base_url=_env("NEXTCLOUD_URL", "http://nextcloud-aio-apache:11000"),
            utilisateur=_env("NEXTCLOUD_USER", "claude-mcp"),
            mot_de_passe=_env("NEXTCLOUD_APP_PASSWORD"),
            fuseau_horaire=_env("AGENDA_FUSEAU", "Europe/Paris"),
            agenda_defaut=os.environ.get("AGENDA_DEFAUT") or None,
            chemin_agendas=os.environ.get("AGENDA_CHEMIN") or None,
            hote=os.environ.get("NEXTCLOUD_HOST") or None,
        )
    mcp, _ = construire(coffre, reglages,
                        description=os.environ.get("COFFRE_DESCRIPTION") or COFFRE_DEFAUT,
                        titre=os.environ.get("COFFRE_TITRE") or "Coffre Obsidian",
                        agenda=agenda)
    hote = urlparse(url_publique).hostname
    return mcp.streamable_http_app(
        json_response=True,     # une réponse JSON par requête : pas de flux SSE à faire traverser au tunnel
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[hote, f"{hote}:*"],
            allowed_origins=["https://claude.ai"],
        ),
    )
