"""Écritures dans Nextcloud par WebDAV, avec le compte dédié « claude-mcp ».

Pourquoi WebDAV et pas une écriture directe dans les fichiers : Nextcloud crée alors une version de la
note (historique), met à jour son index et prévient les appareils synchronisés. Le compte claude-mcp n'a
pas le droit de supprimer : la règle « aucune suppression » est garantie par Nextcloud lui-même.

Concurrence : chaque écriture porte l'ETag lu juste avant (If-Match) ou « If-None-Match: * » pour une
création. Si la note a changé entre-temps (édition sur le Mac, synchro du téléphone), Nextcloud répond
412 et rien n'est écrasé.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

import logging

from .coffre import ConflitEcriture, ErreurCoffre

log = logging.getLogger("coffre_mcp.webdav")


class ClientWebDAV:
    def __init__(self, base_url: str, utilisateur: str, mot_de_passe: str, dossier: str,
                 hote: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        # base_url : ex. http://nextcloud-aio-apache:11000 (réseau Docker interne, sans passer par Internet)
        # hote : nom public attendu par Nextcloud (trusted_domains), ex. cloud.example.com
        en_tetes = {"X-Forwarded-Proto": "https", "OCS-APIRequest": "true"}
        if hote:
            en_tetes["Host"] = hote
        self._racine = f"/remote.php/dav/files/{quote(utilisateur)}/{quote(dossier.strip('/'))}"
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), auth=(utilisateur, mot_de_passe), headers=en_tetes,
            timeout=httpx.Timeout(20.0), transport=transport, follow_redirects=False,
        )

    def _url(self, chemin: str) -> str:
        return self._racine + "/" + "/".join(quote(seg) for seg in chemin.split("/") if seg)

    @staticmethod
    def _echec(r: httpx.Response, action: str) -> ErreurCoffre:
        if r.status_code in (401, 403):
            return ErreurCoffre(f"Nextcloud refuse {action} (droits du compte claude-mcp).")
        if r.status_code == 412:
            return ConflitEcriture(
                "La note a été modifiée entre-temps (par un appareil synchronisé) : relis-la puis recommence."
            )
        if r.status_code == 507:
            return ErreurCoffre("Espace disque insuffisant côté Nextcloud.")
        return ErreurCoffre(f"Nextcloud a répondu {r.status_code} pendant {action}.")

    @staticmethod
    def etag_brut(r: httpx.Response) -> str:
        """ETag utilisable dans If-Match. Apache (mod_deflate) ajoute « -gzip » à l'ETag des réponses qu'il
        compresse, et Nextcloud refuse ensuite cet ETag modifié (412) : on lit donc sans compression, et on
        retire le suffixe s'il apparaît malgré tout. OC-ETag, propre à Nextcloud, n'est jamais modifié."""
        etag = r.headers.get("oc-etag") or r.headers.get("etag", "")
        for suffixe in ("-gzip", "-br", "-deflate"):
            if etag.endswith(suffixe + '"'):
                etag = etag[: -len(suffixe) - 1] + '"'
        return etag

    async def lire(self, chemin: str) -> tuple[str, str] | None:
        try:
            r = await self._client.get(self._url(chemin), headers={"Accept-Encoding": "identity"})
        except httpx.HTTPError:
            raise ErreurCoffre("Nextcloud est injoignable pour le moment.") from None
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            raise self._echec(r, "la lecture")
        return r.content.decode("utf-8", "replace"), self.etag_brut(r)

    async def ecrire(self, chemin: str, contenu: str, *, etag: str | None, creation: bool) -> None:
        en_tetes = {"Content-Type": "text/markdown; charset=utf-8"}
        if creation:
            en_tetes["If-None-Match"] = "*"
        elif etag:
            en_tetes["If-Match"] = etag
        try:
            r = await self._client.put(self._url(chemin), content=contenu.encode("utf-8"), headers=en_tetes)
        except httpx.HTTPError:
            raise ErreurCoffre("Nextcloud est injoignable pour le moment.") from None
        if r.status_code not in (200, 201, 204):
            log.warning("écriture refusée (%s) sur %s, If-Match=%s", r.status_code, chemin, en_tetes.get("If-Match"))
            raise self._echec(r, "l'écriture")

    async def creer_dossiers(self, chemin_dossier: str) -> None:
        courant = ""
        for seg in [s for s in chemin_dossier.split("/") if s]:
            courant = f"{courant}/{seg}" if courant else seg
            try:
                r = await self._client.request("MKCOL", self._url(courant))
            except httpx.HTTPError:
                raise ErreurCoffre("Nextcloud est injoignable pour le moment.") from None
            if r.status_code not in (201, 405):  # 405 = le dossier existe déjà
                raise self._echec(r, "la création du dossier")

    async def fermer(self) -> None:
        await self._client.aclose()
