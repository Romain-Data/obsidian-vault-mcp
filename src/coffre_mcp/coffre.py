"""Accès au coffre Obsidian : chemins autorisés, lecture et recherche en local, écritures via Nextcloud.

Lecture et recherche : directement dans le dossier monté en lecture seule (rapide, ripgrep).
Écritures : via WebDAV avec le compte Nextcloud « claude-mcp », à qui le dossier est partagé SANS droit
de suppression. Nextcloud crée ainsi une version à chaque écriture et prévient les appareils synchronisés.
Le serveur n'expose aucune suppression ; même un bug ne pourrait pas en faire une (droit refusé côté Nextcloud).
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

EXTENSIONS_LISIBLES = {".md", ".txt", ".canvas", ".base", ".csv", ".json"}
EXTENSION_ECRITURE = ".md"
TAILLE_MAX_LECTURE = 140_000  # caractères (limite d'un résultat d'outil côté claude.ai ≈ 150 000)
TAILLE_MAX_ECRITURE = 200_000  # octets


class ErreurCoffre(Exception):
    """Erreur présentée telle quelle à Claude (message en français, sans détail interne)."""


class ConflitEcriture(ErreurCoffre):
    """La note a changé entre la lecture et l'écriture (ETag différent, HTTP 412)."""


ESSAIS_CONFLIT = 3


def normaliser(chemin: str, *, pour_ecrire: bool = False) -> PurePosixPath:
    """Transforme un chemin fourni par Claude en chemin relatif sûr, ou lève ErreurCoffre.

    Refuse : chemin absolu, « .. », segments cachés (dont .obsidian/), caractères de contrôle,
    et pour l'écriture toute extension autre que .md.
    """
    brut = (chemin or "").strip().replace("\\", "/")
    if brut in ("", ".", "/"):
        if pour_ecrire:
            raise ErreurCoffre("Indique le chemin d'une note, par exemple « Journal/2026-11-26.md ».")
        return PurePosixPath(".")
    if brut.startswith("/"):
        raise ErreurCoffre("Le chemin doit être relatif au coffre (pas de « / » au début).")
    if any(ord(c) < 32 for c in brut):
        raise ErreurCoffre("Le chemin contient des caractères invalides.")
    p = PurePosixPath(brut)
    for seg in p.parts:
        if seg in ("..", "."):
            raise ErreurCoffre("Les segments « . » et « .. » sont interdits.")
        if seg.startswith("."):
            raise ErreurCoffre("Les dossiers et fichiers cachés (dont .obsidian) ne sont pas accessibles.")
    if pour_ecrire:
        if p.suffix.lower() != EXTENSION_ECRITURE:
            raise ErreurCoffre("Seules les notes Markdown (.md) peuvent être écrites.")
        if p.name.lower().startswith("workspace"):
            raise ErreurCoffre("Ce nom de fichier est réservé.")
    return p


@dataclass
class Resultat:
    chemin: str
    ligne: int
    texte: str


class Ecrivain(Protocol):
    """Ce dont le coffre a besoin pour écrire (implémenté par webdav.ClientWebDAV, simulé en test)."""

    async def lire(self, chemin: str) -> tuple[str, str] | None: ...  # (contenu, etag) ou None si absent

    async def ecrire(self, chemin: str, contenu: str, *, etag: str | None, creation: bool) -> None: ...

    async def creer_dossiers(self, chemin_dossier: str) -> None: ...


class Coffre:
    def __init__(self, racine: Path, ecrivain: Ecrivain):
        self.racine = racine.resolve()
        self.ecrivain = ecrivain
        self._rg = shutil.which("rg")

    # ---------- chemins ----------
    def _absolu(self, rel: PurePosixPath) -> Path:
        cible = (self.racine / rel).resolve()
        # Défense en profondeur : refuse tout ce qui sortirait du coffre (lien symbolique, etc.)
        if cible != self.racine and self.racine not in cible.parents:
            raise ErreurCoffre("Chemin hors du coffre.")
        return cible

    # ---------- lecture ----------
    def lister(self, dossier: str = "", recursif: bool = False) -> list[str]:
        rel = normaliser(dossier)
        base = self._absolu(rel)
        if not base.is_dir():
            raise ErreurCoffre(f"Dossier introuvable : « {dossier} ».")
        sortie: list[str] = []
        if recursif:
            for d, dirs, fichiers in os.walk(base):
                dirs[:] = sorted(x for x in dirs if not x.startswith("."))
                for f in sorted(fichiers):
                    if not f.startswith("."):
                        sortie.append(str(Path(d, f).relative_to(self.racine)))
        else:
            for e in sorted(base.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                if e.name.startswith("."):
                    continue
                r = str(e.relative_to(self.racine))
                sortie.append(r + "/" if e.is_dir() else r)
        return sortie

    def lire(self, chemin: str) -> str:
        rel = normaliser(chemin)
        f = self._absolu(rel)
        if not f.is_file():
            raise ErreurCoffre(f"Note introuvable : « {chemin} ».")
        if f.suffix.lower() not in EXTENSIONS_LISIBLES:
            raise ErreurCoffre("Ce type de fichier n'est pas lisible ici (texte uniquement).")
        texte = f.read_text(encoding="utf-8", errors="replace")
        if len(texte) > TAILLE_MAX_LECTURE:
            texte = texte[:TAILLE_MAX_LECTURE] + f"\n\n[… note tronquée à {TAILLE_MAX_LECTURE} caractères]"
        return texte

    async def chercher(self, requete: str, dossier: str = "", max_resultats: int = 50) -> list[Resultat]:
        requete = (requete or "").strip()
        if not requete:
            raise ErreurCoffre("La recherche est vide.")
        max_resultats = max(1, min(int(max_resultats), 200))
        base = self._absolu(normaliser(dossier))
        if self._rg:
            return await self._chercher_rg(requete, base, max_resultats)
        return self._chercher_python(requete, base, max_resultats)

    async def _chercher_rg(self, requete: str, base: Path, n: int) -> list[Resultat]:
        proc = await asyncio.create_subprocess_exec(
            self._rg, "--line-number", "--no-heading", "--color=never", "--ignore-case", "--fixed-strings",
            "--max-count=5", "--max-columns=300", "--glob=!.*", "--glob=*.md", "--", requete, str(base),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            sortie, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        except TimeoutError:
            proc.kill()
            raise ErreurCoffre("La recherche a pris trop de temps.") from None
        res: list[Resultat] = []
        for ligne in sortie.decode("utf-8", "replace").splitlines():
            chemin, _, reste = ligne.partition(":")
            num, _, texte = reste.partition(":")
            if not num.isdigit():
                continue
            res.append(Resultat(str(Path(chemin).relative_to(self.racine)), int(num), texte.strip()))
            if len(res) >= n:
                break
        return res

    def _chercher_python(self, requete: str, base: Path, n: int) -> list[Resultat]:
        q = requete.lower()
        res: list[Resultat] = []
        for d, dirs, fichiers in os.walk(base):
            dirs[:] = sorted(x for x in dirs if not x.startswith("."))
            for f in sorted(fichiers):
                if f.startswith(".") or not f.endswith(".md"):
                    continue
                p = Path(d, f)
                trouves = 0
                for i, ligne in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if q in ligne.lower():
                        res.append(Resultat(str(p.relative_to(self.racine)), i, ligne.strip()[:300]))
                        trouves += 1
                        if len(res) >= n:
                            return res
                        if trouves >= 5:
                            break
        return res

    # ---------- écriture (via Nextcloud) ----------
    @staticmethod
    def _verifier_taille(contenu: str) -> None:
        if len(contenu.encode("utf-8")) > TAILLE_MAX_ECRITURE:
            raise ErreurCoffre(f"Contenu trop long (maximum {TAILLE_MAX_ECRITURE // 1000} Ko).")

    async def ecrire_note(self, chemin: str, contenu: str, remplacer_si_existe: bool = False) -> str:
        rel = normaliser(chemin, pour_ecrire=True)
        self._verifier_taille(contenu)
        existant = await self.ecrivain.lire(str(rel))
        if existant is not None and not remplacer_si_existe:
            raise ErreurCoffre(
                f"La note « {rel} » existe déjà. Utilise ajouter_a_note ou remplacer_dans_note, "
                "ou remplacer_si_existe=true pour la réécrire entièrement (Nextcloud garde l'ancienne version)."
            )
        if existant is None and str(rel.parent) != ".":
            await self.ecrivain.creer_dossiers(str(rel.parent))
        await self.ecrivain.ecrire(
            str(rel), contenu, etag=existant[1] if existant else None, creation=existant is None
        )
        return f"Note {'réécrite' if existant else 'créée'} : {rel}"

    async def _modifier(self, rel: PurePosixPath, transformer) -> None:
        """Lire → transformer → écrire si l'ETag n'a pas bougé. En cas de conflit, on RELIT et on réapplique
        la transformation (jusqu'à 3 fois) : une modification faite entre-temps ailleurs est conservée, jamais
        écrasée. Nextcloud renvoie aussi parfois un conflit juste après la création d'une note partagée."""
        for essai in range(1, ESSAIS_CONFLIT + 1):
            existant = await self.ecrivain.lire(str(rel))
            if existant is None:
                raise ErreurCoffre(f"Note introuvable : « {rel} ».")
            contenu, etag = existant
            modifie = transformer(contenu)
            self._verifier_taille(modifie)
            try:
                await self.ecrivain.ecrire(str(rel), modifie, etag=etag, creation=False)
                return
            except ConflitEcriture:
                if essai == ESSAIS_CONFLIT:
                    raise
                await asyncio.sleep(essai)

    async def ajouter_a_note(self, chemin: str, texte: str) -> str:
        rel = normaliser(chemin, pour_ecrire=True)

        def ajouter(contenu: str) -> str:
            separateur = "" if contenu.endswith("\n") or not contenu else "\n"
            return contenu + separateur + texte.rstrip("\n") + "\n"

        await self._modifier(rel, ajouter)
        return f"Texte ajouté à la fin de : {rel}"

    async def remplacer_dans_note(self, chemin: str, ancien: str, nouveau: str) -> str:
        rel = normaliser(chemin, pour_ecrire=True)
        if not ancien:
            raise ErreurCoffre("Le texte à remplacer est vide.")

        def remplacer(contenu: str) -> str:
            n = contenu.count(ancien)
            if n == 0:
                raise ErreurCoffre("Le texte à remplacer est introuvable dans la note (il doit correspondre exactement).")
            if n > 1:
                raise ErreurCoffre(f"Le texte à remplacer apparaît {n} fois : ajoute du contexte pour qu'il soit unique.")
            return contenu.replace(ancien, nouveau, 1)

        await self._modifier(rel, remplacer)
        return f"Remplacement effectué dans : {rel}"
