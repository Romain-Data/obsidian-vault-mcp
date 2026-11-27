"""Agenda : tests contre un vrai serveur CalDAV (Radicale, lancé localement) + cas propres à Nextcloud simulés."""

import socket
import subprocess
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from coffre_mcp.agenda import ClientCalDAV, lire_moment
from coffre_mcp.coffre import ConflitEcriture, ErreurCoffre

PARIS = ZoneInfo("Europe/Paris")


def _port_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def radicale(tmp_path_factory):
    d = tmp_path_factory.mktemp("radicale")
    (d / "users").write_text("claude:secret\n")
    port = _port_libre()
    (d / "config").write_text(f"""
[server]
hosts = 127.0.0.1:{port}
[auth]
type = htpasswd
htpasswd_filename = {d / 'users'}
htpasswd_encryption = plain
[rights]
type = owner_only
[storage]
filesystem_folder = {d / 'stockage'}
[logging]
level = warning
""")
    proc = subprocess.Popen([sys.executable, "-m", "radicale", "--config", str(d / "config")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(url, timeout=0.2)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    for slug, nom in (("perso", "Perso"), ("famille", "Famille")):
        r = httpx.request("MKCALENDAR", f"{url}/claude/{slug}/", auth=("claude", "secret"), content=f"""<?xml version="1.0"?>
<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>
<d:displayname>{nom}</d:displayname></d:prop></d:set></c:mkcalendar>""")
        assert r.status_code == 201, r.text
    yield url
    proc.terminate()
    proc.wait(5)


@pytest.fixture
async def agenda(radicale):
    c = ClientCalDAV(radicale, "claude", "secret", agenda_defaut="Perso", chemin_agendas="/claude/")
    yield c
    await c.fermer()


def test_lire_moment():
    assert lire_moment("2026-11-29", PARIS) == date(2026, 11, 29)
    assert lire_moment("2026-11-29T10:00", PARIS) == datetime(2026, 11, 29, 10, tzinfo=PARIS)
    assert lire_moment("2026-11-29 10:00", PARIS) == datetime(2026, 11, 29, 10, tzinfo=PARIS)
    assert lire_moment("2026-11-29T09:00Z", PARIS) == datetime(2026, 11, 29, 10, tzinfo=PARIS)
    with pytest.raises(ErreurCoffre):
        lire_moment("dimanche", PARIS)


async def test_agendas(agenda):
    texte = await agenda.lister_agendas()
    assert "Famille (id : famille) · modifiable" in texte
    assert "Perso (id : perso) · modifiable · par défaut" in texte


async def test_creer_lister_modifier(agenda):
    msg = await agenda.creer("Batch cooking", "2026-11-29T10:00", "2026-11-29T12:00", lieu="Cuisine",
                             rappel_minutes=30)
    assert "dim. 29/11/2026 10:00–12:00 · Batch cooking · 📍 Cuisine" in msg
    liste = await agenda.lister("2026-11-29")
    assert "10:00–12:00 · Batch cooking" in liste
    id_ev = liste.split("id : ")[1].rstrip("]")
    assert id_ev.startswith("perso/") and id_ev.endswith(".ics")

    # Déplacer le début garde la durée ; le titre change, le lieu reste
    msg = await agenda.modifier(id_ev, debut="2026-11-29T14:00", titre="Batch cooking (soupes)")
    assert "14:00–16:00 · Batch cooking (soupes) · 📍 Cuisine" in msg
    # Effacer le lieu, retirer le rappel
    msg = await agenda.modifier(id_ev, lieu="", rappel_minutes=-1)
    assert "📍" not in msg
    cal, _ = await agenda._lire_fichier((await agenda.agendas())[1], id_ev.split("/")[1])
    assert not list(cal.walk("VALARM"))
    assert "BEGIN:VTIMEZONE" in cal.to_ical().decode()


async def test_recurrence_et_journee_entiere(agenda):
    await agenda.creer("Courses", "2026-12-05T09:30", recurrence="FREQ=WEEKLY;BYDAY=SA", agenda="famille")
    await agenda.creer("Vacances Saint-Yorre", "2026-12-21", "2026-12-24", journee_entiere=True)
    liste = await agenda.lister("2026-12-01", "2026-12-31")
    assert liste.count("Courses") == 4  # 5, 12, 19, 26 décembre
    assert "sam. 05/12/2026 09:30–10:30 · Courses · (récurrent)" in liste
    assert "lun. 21/12/2026 → jeu. 24/12/2026 · Vacances Saint-Yorre" in liste
    # Filtre par agenda
    assert "Vacances" not in await agenda.lister("2026-12-01", "2026-12-31", "Famille")
    # Allonger d'un jour une journée entière : fin = dernier jour inclus
    id_vac = next(l for l in liste.splitlines() if "Vacances" in l).split("id : ")[1].rstrip("]")
    assert "→ ven. 25/12/2026" in await agenda.modifier(id_vac, fin="2026-12-25")
    # Décaler la série : garde la durée et la règle
    id_courses = next(l for l in liste.splitlines() if "Courses" in l).split("id : ")[1].rstrip("]")
    await agenda.modifier(id_courses, debut="2026-12-05T10:00")
    assert (await agenda.lister("2026-12-12")).startswith("sam. 12/12/2026 10:00–11:00 · Courses")


async def test_refus(agenda):
    with pytest.raises(ErreurCoffre, match="Plusieurs agendas"):
        await ClientCalDAV(agenda._client.base_url.__str__(), "claude", "secret",
                           chemin_agendas="/claude/").creer("X", "2026-12-01T10:00")
    with pytest.raises(ErreurCoffre, match="Agenda introuvable"):
        await agenda.creer("X", "2026-12-01T10:00", agenda="Travail")
    with pytest.raises(ErreurCoffre, match="après le début"):
        await agenda.creer("X", "2026-12-01T10:00", "2026-12-01T09:00")
    with pytest.raises(ErreurCoffre, match="Récurrence invalide"):
        await agenda.creer("X", "2026-12-01T10:00", recurrence="tous les jours")
    with pytest.raises(ErreurCoffre, match="Identifiant"):
        await agenda.modifier("perso/../../autre.ics", titre="X")
    with pytest.raises(ErreurCoffre, match="Rien à modifier"):
        await agenda.modifier("perso/x.ics")
    with pytest.raises(ErreurCoffre, match="introuvable"):
        await agenda.modifier("perso/nexistepas.ics", titre="X")
    with pytest.raises(ErreurCoffre, match="trop longue"):
        await agenda.lister("2026-01-01", "2027-12-31")


async def test_conflit_detecte(agenda):
    """If-Match : une écriture basée sur une version périmée est refusée, jamais écrasée."""
    msg = await agenda.creer("Tennis", "2026-12-02T19:00")
    id_ev = msg.split("id : ")[1].rstrip("]")
    a = (await agenda.agendas())[1]
    cal, etag = await agenda._lire_fichier(a, id_ev.split("/")[1])
    await agenda.modifier(id_ev, lieu="TOEC")  # quelqu'un d'autre modifie entre-temps
    with pytest.raises(ConflitEcriture):
        await agenda._ecrire_fichier(a, id_ev.split("/")[1], cal, etag=etag)


async def test_agenda_partage_nextcloud_en_lecture_seule():
    """Réponse PROPFIND façon Nextcloud : agenda partagé en lecture seule, liste de tâches, carnet hors-sujet."""
    xml = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
 <d:response><d:href>/remote.php/dav/calendars/claude-mcp/</d:href><d:propstat><d:prop>
  <d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/calendars/claude-mcp/personnel_shared_by_romain/</d:href><d:propstat><d:prop>
  <d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Personnel (Romain)</d:displayname>
  <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
  <d:current-user-privilege-set><d:privilege><d:read/></d:privilege><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>
 </d:prop></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/calendars/claude-mcp/anniversaires_shared_by_romain/</d:href><d:propstat><d:prop>
  <d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Anniversaires</d:displayname>
  <c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>
  <d:current-user-privilege-set><d:privilege><d:read/></d:privilege></d:current-user-privilege-set>
 </d:prop></d:propstat></d:response>
 <d:response><d:href>/remote.php/dav/calendars/claude-mcp/taches/</d:href><d:propstat><d:prop>
  <d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>Tâches</d:displayname>
  <c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>
 </d:prop></d:propstat></d:response>
</d:multistatus>"""
    vus = []

    def nextcloud(req: httpx.Request) -> httpx.Response:
        vus.append((req.method, req.url.path, req.headers.get("host")))
        return httpx.Response(207, content=xml.encode())

    c = ClientCalDAV("http://nc", "claude-mcp", "x", hote="cloud.example.com",
                     transport=httpx.MockTransport(nextcloud))
    texte = await c.lister_agendas()
    assert vus[0] == ("PROPFIND", "/remote.php/dav/calendars/claude-mcp/", "cloud.example.com")
    assert "Personnel (Romain) (id : personnel_shared_by_romain) · modifiable" in texte
    assert "Anniversaires (id : anniversaires_shared_by_romain) · lecture seule" in texte
    assert "Tâches" not in texte
    # Un seul agenda modifiable → choisi sans qu'on le nomme ; l'agenda en lecture seule est refusé
    assert (await c._agenda(None, pour_ecrire=True)).slug == "personnel_shared_by_romain"
    with pytest.raises(ErreurCoffre, match="lecture seule"):
        await c._agenda("Anniversaires", pour_ecrire=True)


def _reglages(tmp_path):
    from coffre_mcp.auth import ReglagesAuth, hacher_phrase
    return ReglagesAuth(url_publique="https://mcp.test", client_id="c", client_secret="s",
                        redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
                        empreinte_phrase=hacher_phrase("phrase", n=2**10), totp_secret="JBSWY3DPEHPK3PXP",
                        fichier_base=tmp_path / "oauth.sqlite3")


async def test_outils_agenda_optionnels(tmp_path, agenda):
    from coffre_mcp.coffre import Coffre
    from coffre_mcp.server import construire
    from test_coffre import FauxNextcloud

    (tmp_path / "c").mkdir()
    coffre = Coffre(tmp_path / "c", FauxNextcloud(tmp_path / "c"))
    sans, _ = construire(coffre, _reglages(tmp_path))
    noms = {o.name for o in sans._tool_manager.list_tools()}
    assert not noms & {"lister_agendas", "lister_evenements", "creer_evenement", "modifier_evenement"}

    avec, _ = construire(coffre, _reglages(tmp_path), agenda=agenda)
    noms = {o.name for o in avec._tool_manager.list_tools()}
    assert {"lister_agendas", "lister_evenements", "creer_evenement", "modifier_evenement"} <= noms
    assert "Europe/Paris" in avec.instructions
    # Bout en bout par l'outil MCP : création puis lecture
    await avec._tool_manager.call_tool("creer_evenement", {"titre": "Anniversaire", "debut": "2027-02-27"}, None)
    resultat = await avec._tool_manager.call_tool("lister_evenements", {"debut": "2027-02-27"}, None)
    assert "sam. 27/02/2027, journée entière · Anniversaire" in str(resultat)
