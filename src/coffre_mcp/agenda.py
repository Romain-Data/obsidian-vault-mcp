"""Agenda : lecture et écriture d'événements dans les agendas Nextcloud, par CalDAV.

Même compte que pour les notes (« claude-mcp ») : l'utilisateur lui partage son agenda avec le droit
« Peut modifier ». Les événements arrivent ensuite sur le téléphone par DAVx⁵ (ou tout client CalDAV).

Choix :
- Pas d'outil de suppression (même règle que pour les notes). Attention, contrairement aux notes, ce n'est
  garanti que par le code : un partage d'agenda Nextcloud en écriture autorise aussi la suppression.
  La corbeille de l'agenda Nextcloud reste le filet de sécurité.
- Concurrence : création en « If-None-Match: * », modification en « If-Match » sur l'ETag lu juste avant ;
  en cas de conflit (412), on relit et on réapplique, comme pour les notes.
- Les récurrences sont développées côté serveur MCP (recurring-ical-events) plutôt que par le serveur
  CalDAV : le résultat ne dépend pas du support, variable, de « expand » selon les serveurs.
- Heures : saisies et affichées dans le fuseau de l'utilisateur (AGENDA_FUSEAU, Europe/Paris par défaut).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import recurring_ical_events
from icalendar import Alarm, Calendar, Event, Timezone, vRecur

from .coffre import ConflitEcriture, ErreurCoffre

log = logging.getLogger("coffre_mcp.agenda")

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/",
      "x1": "http://apple.com/ns/ical/"}
PRODID = "-//coffre-mcp//agenda//FR"
MAX_JOURS = 366
MAX_EVENEMENTS = 200
ESSAIS_CONFLIT = 3
JOURS = ["lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim."]

PROPFIND_AGENDAS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
    <d:resourcetype/><d:displayname/><c:supported-calendar-component-set/><d:current-user-privilege-set/>
  </d:prop>
</d:propfind>"""

REPORT_PLAGE = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT"><c:time-range start="{debut}" end="{fin}"/></c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""


@dataclass
class Agenda:
    slug: str          # dernier segment de l'URL : identifiant stable
    nom: str           # nom affiché
    href: str          # chemin absolu côté serveur, finit par « / »
    modifiable: bool


@dataclass
class Occurrence:
    id: str            # « slug/fichier.ics » : ce qu'il faut passer à modifier_evenement
    agenda: str
    titre: str
    debut: datetime | date
    fin: datetime | date | None
    journee: bool
    lieu: str
    recurrent: bool


# ---------- dates ----------
def fuseau(nom: str) -> ZoneInfo:
    try:
        return ZoneInfo(nom)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f"Fuseau horaire inconnu : {nom}") from None


def lire_moment(texte: str, tz: ZoneInfo) -> datetime | date:
    """« 2026-11-29 » → date (journée entière) ; « 2026-11-29T10:00 » ou « 2026-11-29 10:00 » → heure locale ;
    un décalage explicite (« +01:00 », « Z ») est respecté puis converti dans le fuseau de l'utilisateur."""
    t = (texte or "").strip()
    if not t:
        raise ErreurCoffre("Date manquante.")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        try:
            return date.fromisoformat(t)
        except ValueError:
            raise ErreurCoffre(f"Date invalide : « {texte} ».") from None
    try:
        dt = datetime.fromisoformat(t.replace(" ", "T", 1).replace("Z", "+00:00"))
    except ValueError:
        raise ErreurCoffre(f"Date invalide : « {texte} » (format attendu : 2026-11-29 ou 2026-11-29T10:00).") from None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _en_datetime(m: datetime | date, tz: ZoneInfo) -> datetime:
    if isinstance(m, datetime):
        return m if m.tzinfo else m.replace(tzinfo=tz)
    return datetime.combine(m, time(0, 0), tzinfo=tz)


def _utc_caldav(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _format(o: Occurrence, tz: ZoneInfo) -> str:
    if o.journee:
        d = o.debut if not isinstance(o.debut, datetime) else o.debut.date()
        derniere = (o.fin - timedelta(days=1)) if isinstance(o.fin, date) and not isinstance(o.fin, datetime) else d
        quand = f"{JOURS[d.weekday()]} {d:%d/%m/%Y}, journée entière"
        if derniere > d:
            quand = f"{JOURS[d.weekday()]} {d:%d/%m/%Y} → {JOURS[derniere.weekday()]} {derniere:%d/%m/%Y}"
    else:
        deb = _en_datetime(o.debut, tz).astimezone(tz)
        quand = f"{JOURS[deb.weekday()]} {deb:%d/%m/%Y %H:%M}"
        if isinstance(o.fin, datetime):
            fin = o.fin.astimezone(tz) if o.fin.tzinfo else o.fin.replace(tzinfo=tz)
            quand += f"–{fin:%H:%M}" if fin.date() == deb.date() else f" → {fin:%d/%m %H:%M}"
    morceaux = [quand, o.titre or "(sans titre)"]
    if o.lieu:
        morceaux.append(f"📍 {o.lieu}")
    if o.recurrent:
        morceaux.append("(récurrent)")
    morceaux.append(f"[agenda : {o.agenda} · id : {o.id}]")
    return " · ".join(morceaux)


# ---------- iCalendar ----------
def _vtimezone(tz: ZoneInfo) -> Timezone | None:
    try:
        return Timezone.from_tzid(tz.key)
    except Exception:  # fuseau absent de la base : les clients savent généralement le résoudre seuls
        return None


def _regle(recurrence: str) -> vRecur:
    """Accepte une RRULE (« FREQ=WEEKLY;BYDAY=SU ») ; refuse ce qui n'en est pas une."""
    r = (recurrence or "").strip()
    if r.upper().startswith("RRULE:"):
        r = r[6:]
    if "FREQ=" not in r.upper():
        raise ErreurCoffre("Récurrence invalide : donne une règle RRULE, ex. « FREQ=WEEKLY;BYDAY=SU ».")
    try:
        return vRecur.from_ical(r)
    except Exception:
        raise ErreurCoffre(f"Récurrence invalide : « {recurrence} ».") from None


def _poser_dates(ev: Event, debut: datetime | date, fin: datetime | date | None, journee: bool) -> None:
    for cle in ("DTSTART", "DTEND", "DURATION"):
        ev.pop(cle, None)
    if journee:
        d = debut.date() if isinstance(debut, datetime) else debut
        f = fin.date() if isinstance(fin, datetime) else fin
        # DTEND exclusif : un événement « dimanche » finit lundi 00:00
        f = (f + timedelta(days=1)) if f is not None and f >= d else d + timedelta(days=1)
        ev.add("DTSTART", d)
        ev.add("DTEND", f)
        return
    if not isinstance(debut, datetime):
        raise ErreurCoffre("Indique une heure de début (ex. 2026-11-29T10:00), ou journee_entiere=true.")
    if fin is None:
        fin = debut + timedelta(hours=1)
    if not isinstance(fin, datetime):
        raise ErreurCoffre("La fin doit avoir une heure (ex. 2026-11-29T12:00).")
    if fin <= debut:
        raise ErreurCoffre("La fin doit être après le début.")
    ev.add("DTSTART", debut)
    ev.add("DTEND", fin)


def _poser_rappel(ev: Event, minutes: int | None) -> None:
    for a in [c for c in ev.subcomponents if c.name == "VALARM"]:
        ev.subcomponents.remove(a)
    if minutes is None or minutes < 0:
        return
    alarme = Alarm()
    alarme.add("ACTION", "DISPLAY")
    alarme.add("DESCRIPTION", str(ev.get("SUMMARY", "Rappel")))
    alarme.add("TRIGGER", timedelta(minutes=-int(minutes)))
    ev.add_component(alarme)


def _ajouter_fuseaux(cal: Calendar, tz: ZoneInfo) -> None:
    deja = {str(c.get("TZID")) for c in cal.walk("VTIMEZONE")}
    if tz.key in deja:
        return
    for ev in cal.walk("VEVENT"):
        dt = ev.get("DTSTART")
        if dt is not None and isinstance(dt.dt, datetime) and dt.dt.tzinfo is not None:
            vtz = _vtimezone(tz)
            if vtz is not None:
                cal.subcomponents.insert(0, vtz)
            return


# ---------- client CalDAV ----------
class ClientCalDAV:
    def __init__(self, base_url: str, utilisateur: str, mot_de_passe: str, *, fuseau_horaire: str = "Europe/Paris",
                 agenda_defaut: str | None = None, chemin_agendas: str | None = None, hote: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        en_tetes = {"X-Forwarded-Proto": "https"}
        if hote:
            en_tetes["Host"] = hote
        self.tz = fuseau(fuseau_horaire)
        self.agenda_defaut = (agenda_defaut or "").strip() or None
        self._accueil = chemin_agendas or f"/remote.php/dav/calendars/{quote(utilisateur)}/"
        if not self._accueil.endswith("/"):
            self._accueil += "/"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), auth=(utilisateur, mot_de_passe), headers=en_tetes,
            timeout=httpx.Timeout(20.0), transport=transport, follow_redirects=False,
        )
        self._cache: list[Agenda] | None = None

    async def fermer(self) -> None:
        await self._client.aclose()

    async def _requete(self, methode: str, url: str, action: str, **kw) -> httpx.Response:
        try:
            r = await self._client.request(methode, url, **kw)
        except httpx.HTTPError:
            raise ErreurCoffre("Nextcloud est injoignable pour le moment.") from None
        if r.status_code in (401, 403):
            raise ErreurCoffre(f"Nextcloud refuse {action} (droits du compte sur cet agenda).")
        if r.status_code == 412:
            raise ConflitEcriture("L'événement a été modifié entre-temps : relis-le puis recommence.")
        return r

    # --- agendas ---
    async def agendas(self, rafraichir: bool = False) -> list[Agenda]:
        if self._cache is not None and not rafraichir:
            return self._cache
        r = await self._requete("PROPFIND", self._accueil, "la liste des agendas", content=PROPFIND_AGENDAS,
                                headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
        if r.status_code != 207:
            raise ErreurCoffre(f"Nextcloud a répondu {r.status_code} pendant la liste des agendas.")
        res: list[Agenda] = []
        for rep in ET.fromstring(r.content).findall("d:response", NS):
            href = unquote(rep.findtext("d:href", "", NS))
            prop = rep.find("d:propstat/d:prop", NS)
            if prop is None or prop.find("d:resourcetype/c:calendar", NS) is None:
                continue
            composants = [e.get("name") for e in prop.findall("c:supported-calendar-component-set/c:comp", NS)]
            if composants and "VEVENT" not in composants:
                continue  # listes de tâches seules
            droits = {e.tag.split("}")[1] for e in prop.findall("d:current-user-privilege-set/d:privilege/*", NS)}
            modifiable = bool(droits & {"write", "write-content", "all"}) if droits else True
            if not href.endswith("/"):
                href += "/"
            slug = href.rstrip("/").rsplit("/", 1)[-1]
            res.append(Agenda(slug, (prop.findtext("d:displayname", "", NS) or slug).strip(), href, modifiable))
        self._cache = sorted(res, key=lambda a: a.nom.lower())
        return self._cache

    async def _agenda(self, nom: str | None, *, pour_ecrire: bool) -> Agenda:
        tous = await self.agendas()
        candidats = [a for a in tous if a.modifiable] if pour_ecrire else tous
        cible = (nom or "").strip() or (self.agenda_defaut if pour_ecrire else None)
        if cible:
            c = cible.lower()
            for a in tous:
                if c in (a.slug.lower(), a.nom.lower()):
                    if pour_ecrire and not a.modifiable:
                        raise ErreurCoffre(f"L'agenda « {a.nom} » est en lecture seule pour ce compte.")
                    return a
            raise ErreurCoffre(f"Agenda introuvable : « {cible} ». Disponibles : "
                               + ", ".join(f"{a.nom} ({a.slug})" for a in tous) + ".")
        if len(candidats) == 1:
            return candidats[0]
        if not candidats:
            raise ErreurCoffre("Aucun agenda modifiable : partage un agenda à ce compte avec le droit « Peut modifier ».")
        raise ErreurCoffre("Plusieurs agendas : précise lequel (" + ", ".join(a.nom for a in candidats) + ").")

    def _par_id(self, id_evenement: str, tous: list[Agenda]) -> tuple[Agenda, str]:
        slug, _, fichier = (id_evenement or "").strip().partition("/")
        if not fichier or "/" in fichier or not fichier.endswith(".ics") or fichier.startswith("."):
            raise ErreurCoffre("Identifiant d'événement invalide : reprends l'id donné par lister_evenements.")
        for a in tous:
            if a.slug == slug:
                return a, fichier
        raise ErreurCoffre(f"Agenda introuvable : « {slug} ».")

    # --- lecture ---
    async def evenements(self, debut: str, fin: str | None = None, agenda: str | None = None) -> list[Occurrence]:
        d = lire_moment(debut, self.tz)
        f = lire_moment(fin, self.tz) if fin else None
        d_dt = _en_datetime(d, self.tz)
        if f is None:
            f_dt = d_dt + timedelta(days=1)
        else:
            f_dt = _en_datetime(f, self.tz)
            if not isinstance(f, datetime):
                f_dt += timedelta(days=1)  # « jusqu'au 30/11 » inclut le 30
        if f_dt <= d_dt:
            raise ErreurCoffre("La fin de la période doit être après son début.")
        if f_dt - d_dt > timedelta(days=MAX_JOURS):
            raise ErreurCoffre(f"Période trop longue (maximum {MAX_JOURS} jours).")
        cibles = [await self._agenda(agenda, pour_ecrire=False)] if agenda else await self.agendas()
        corps = REPORT_PLAGE.format(debut=_utc_caldav(d_dt), fin=_utc_caldav(f_dt))
        res: list[Occurrence] = []
        for a in cibles:
            r = await self._requete("REPORT", a.href, "la lecture de l'agenda", content=corps,
                                    headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"})
            if r.status_code != 207:
                raise ErreurCoffre(f"Nextcloud a répondu {r.status_code} pendant la lecture de « {a.nom} ».")
            for rep in ET.fromstring(r.content).findall("d:response", NS):
                donnees = rep.findtext("d:propstat/d:prop/c:calendar-data", "", NS)
                if not donnees:
                    continue
                fichier = unquote(rep.findtext("d:href", "", NS)).rstrip("/").rsplit("/", 1)[-1]
                try:
                    cal = Calendar.from_ical(donnees)
                    occurrences = recurring_ical_events.of(cal).between(d_dt, f_dt)
                except Exception:
                    log.warning("événement illisible ignoré : %s/%s", a.slug, fichier)
                    continue
                recurrent = any(e.get("RRULE") or e.get("RECURRENCE-ID") for e in cal.walk("VEVENT"))
                for ev in occurrences:
                    if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                        continue
                    dd = ev.get("DTSTART").dt
                    fd = ev.get("DTEND").dt if ev.get("DTEND") is not None else None
                    res.append(Occurrence(f"{a.slug}/{fichier}", a.nom, str(ev.get("SUMMARY", "")), dd, fd,
                                          not isinstance(dd, datetime), str(ev.get("LOCATION", "")), recurrent))
        res.sort(key=lambda o: _en_datetime(o.debut, self.tz))
        return res[:MAX_EVENEMENTS]

    async def lister(self, debut: str, fin: str | None = None, agenda: str | None = None) -> str:
        occ = await self.evenements(debut, fin, agenda)
        if not occ:
            return "Aucun événement sur cette période."
        lignes = [_format(o, self.tz) for o in occ]
        if len(occ) == MAX_EVENEMENTS:
            lignes.append(f"[… limité à {MAX_EVENEMENTS} événements : réduis la période]")
        return "\n".join(lignes)

    async def lister_agendas(self) -> str:
        tous = await self.agendas(rafraichir=True)
        if not tous:
            return "Aucun agenda visible : partage un agenda Nextcloud à ce compte."
        defaut = (self.agenda_defaut or "").lower()
        return "\n".join(
            f"{a.nom} (id : {a.slug}) · {'modifiable' if a.modifiable else 'lecture seule'}"
            + (" · par défaut" if defaut in (a.slug.lower(), a.nom.lower()) else "")
            for a in tous
        )

    # --- écriture ---
    async def _lire_fichier(self, a: Agenda, fichier: str) -> tuple[Calendar, str]:
        r = await self._requete("GET", a.href + quote(fichier), "la lecture de l'événement",
                                headers={"Accept-Encoding": "identity"})
        if r.status_code == 404:
            raise ErreurCoffre("Événement introuvable (supprimé ou déplacé ?) : relance lister_evenements.")
        if r.status_code != 200:
            raise ErreurCoffre(f"Nextcloud a répondu {r.status_code} pendant la lecture de l'événement.")
        etag = r.headers.get("etag", "")
        for suffixe in ("-gzip", "-br", "-deflate"):
            if etag.endswith(suffixe + '"'):
                etag = etag[: -len(suffixe) - 1] + '"'
        return Calendar.from_ical(r.content), etag

    async def _ecrire_fichier(self, a: Agenda, fichier: str, cal: Calendar, *, etag: str | None) -> None:
        en_tetes = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag is None:
            en_tetes["If-None-Match"] = "*"
        elif etag:
            en_tetes["If-Match"] = etag
        r = await self._requete("PUT", a.href + quote(fichier), "l'écriture de l'événement",
                                content=cal.to_ical(), headers=en_tetes)
        if r.status_code not in (200, 201, 204):
            log.warning("écriture d'événement refusée (%s) : %s", r.status_code, r.text[:300])
            raise ErreurCoffre(f"Nextcloud a refusé l'événement ({r.status_code}).")

    async def creer(self, titre: str, debut: str, fin: str | None = None, *, agenda: str | None = None,
                    journee_entiere: bool = False, lieu: str = "", description: str = "",
                    rappel_minutes: int | None = None, recurrence: str = "") -> str:
        titre = (titre or "").strip()
        if not titre:
            raise ErreurCoffre("Le titre est vide.")
        a = await self._agenda(agenda, pour_ecrire=True)
        d = lire_moment(debut, self.tz)
        f = lire_moment(fin, self.tz) if fin else None
        journee = journee_entiere or (not isinstance(d, datetime) and (f is None or not isinstance(f, datetime)))
        uid = f"{uuid.uuid4()}@coffre-mcp"
        ev = Event()
        ev.add("UID", uid)
        ev.add("DTSTAMP", datetime.now(timezone.utc))
        ev.add("CREATED", datetime.now(timezone.utc))
        ev.add("SUMMARY", titre)
        _poser_dates(ev, d, f, journee)
        if lieu.strip():
            ev.add("LOCATION", lieu.strip())
        if description.strip():
            ev.add("DESCRIPTION", description.strip())
        if recurrence.strip():
            ev.add("RRULE", _regle(recurrence))
        _poser_rappel(ev, rappel_minutes)
        cal = Calendar()
        cal.add("PRODID", PRODID)
        cal.add("VERSION", "2.0")
        cal.add_component(ev)
        _ajouter_fuseaux(cal, self.tz)
        fichier = uid.split("@")[0] + ".ics"
        await self._ecrire_fichier(a, fichier, cal, etag=None)
        o = Occurrence(f"{a.slug}/{fichier}", a.nom, titre, ev["DTSTART"].dt, ev["DTEND"].dt, journee, lieu.strip(),
                       bool(recurrence.strip()))
        return "Événement créé : " + _format(o, self.tz)

    async def modifier(self, id_evenement: str, *, titre: str | None = None, debut: str | None = None,
                       fin: str | None = None, journee_entiere: bool | None = None, lieu: str | None = None,
                       description: str | None = None, rappel_minutes: int | None = None,
                       recurrence: str | None = None) -> str:
        a, fichier = self._par_id(id_evenement, await self.agendas())
        if not a.modifiable:
            raise ErreurCoffre(f"L'agenda « {a.nom} » est en lecture seule pour ce compte.")
        if all(v is None for v in (titre, debut, fin, journee_entiere, lieu, description, rappel_minutes, recurrence)):
            raise ErreurCoffre("Rien à modifier : indique au moins un champ.")
        for essai in range(1, ESSAIS_CONFLIT + 1):
            cal, etag = await self._lire_fichier(a, fichier)
            maitres = [e for e in cal.walk("VEVENT") if e.get("RECURRENCE-ID") is None]
            if not maitres:
                raise ErreurCoffre("Cet événement n'a pas de forme modifiable ici.")
            ev = maitres[0]
            if titre is not None:
                if not titre.strip():
                    raise ErreurCoffre("Le titre ne peut pas être vide.")
                ev.pop("SUMMARY", None)
                ev.add("SUMMARY", titre.strip())
            if debut is not None or fin is not None or journee_entiere is not None:
                ancien_d = ev["DTSTART"].dt
                ancien_f = ev["DTEND"].dt if ev.get("DTEND") is not None else None
                if ancien_f is None and ev.get("DURATION") is not None:
                    ancien_f = ancien_d + ev["DURATION"].dt
                ancienne_journee = not isinstance(ancien_d, datetime)
                if ancienne_journee and ancien_f is not None:
                    ancien_f = ancien_f - timedelta(days=1)  # DTEND stocké exclusif → dernier jour inclus
                journee = journee_entiere if journee_entiere is not None else ancienne_journee
                nd = lire_moment(debut, self.tz) if debut is not None else ancien_d
                if fin is not None:
                    nf = lire_moment(fin, self.tz)
                elif debut is None:
                    nf = ancien_f
                elif ancien_f is not None and isinstance(nd, datetime) == isinstance(ancien_d, datetime):
                    nf = nd + (ancien_f - ancien_d)  # déplacer le début garde la durée
                else:
                    nf = None
                if not journee and not isinstance(nd, datetime):
                    raise ErreurCoffre("Indique une heure de début (ex. 2026-11-29T10:00).")
                _poser_dates(ev, nd, nf, journee)
            for cle, valeur in (("LOCATION", lieu), ("DESCRIPTION", description)):
                if valeur is not None:
                    ev.pop(cle, None)
                    if valeur.strip():
                        ev.add(cle, valeur.strip())
            if recurrence is not None:
                ev.pop("RRULE", None)
                if recurrence.strip():
                    ev.add("RRULE", _regle(recurrence))
            if rappel_minutes is not None:
                _poser_rappel(ev, rappel_minutes)
            for cle in ("DTSTAMP", "LAST-MODIFIED"):
                ev.pop(cle, None)
                ev.add(cle, datetime.now(timezone.utc))
            seq = int(ev.get("SEQUENCE", 0)) + 1
            ev.pop("SEQUENCE", None)
            ev.add("SEQUENCE", seq)
            _ajouter_fuseaux(cal, self.tz)
            try:
                await self._ecrire_fichier(a, fichier, cal, etag=etag)
                break
            except ConflitEcriture:
                if essai == ESSAIS_CONFLIT:
                    raise
                await asyncio.sleep(essai)
        dd = ev["DTSTART"].dt
        o = Occurrence(f"{a.slug}/{fichier}", a.nom, str(ev.get("SUMMARY", "")), dd,
                       ev["DTEND"].dt if ev.get("DTEND") is not None else None, not isinstance(dd, datetime),
                       str(ev.get("LOCATION", "")), ev.get("RRULE") is not None)
        return "Événement modifié : " + _format(o, self.tz)
