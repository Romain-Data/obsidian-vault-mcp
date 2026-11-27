import shutil

import pytest

from coffre_mcp.coffre import Coffre, ConflitEcriture, ErreurCoffre, normaliser


class FauxNextcloud:
    """Simule le compte claude-mcp : lecture/écriture avec ETag, sans aucune suppression."""

    def __init__(self, racine):
        self.racine = racine
        self.versions = {}

    def _etag(self, chemin):
        return f'"{self.versions.get(chemin, 0)}"'

    async def lire(self, chemin):
        f = self.racine / chemin
        return (f.read_text(), self._etag(chemin)) if f.exists() else None

    async def ecrire(self, chemin, contenu, *, etag, creation):
        f = self.racine / chemin
        if creation and f.exists():
            raise ErreurCoffre("412")
        if not creation and etag != self._etag(chemin):
            raise ConflitEcriture("La note a été modifiée entre-temps")
        f.write_text(contenu)
        self.versions[chemin] = self.versions.get(chemin, 0) + 1

    async def creer_dossiers(self, d):
        (self.racine / d).mkdir(parents=True, exist_ok=True)


@pytest.fixture
def coffre(tmp_path):
    (tmp_path / ".obsidian").mkdir()
    (tmp_path / ".obsidian" / "app.json").write_text("{}")
    (tmp_path / "Projets").mkdir()
    (tmp_path / "Projets" / "Projet MCP.md").write_text("# MCP\nServeur maison\nOAuth à faire\n")
    (tmp_path / "_index.md").write_text("Index du coffre\n")
    c = Coffre(tmp_path, FauxNextcloud(tmp_path))
    c._rg = None  # recherche Python en test (ripgrep est dans l'image Docker)
    return c


@pytest.mark.parametrize("chemin", ["/etc/passwd", "../x.md", "a/../../x.md", ".obsidian/app.json",
                                    "Projets/.cache/x.md", "a\x00b.md", "a\nb.md"])
def test_chemins_refuses(chemin):
    with pytest.raises(ErreurCoffre):
        normaliser(chemin, pour_ecrire=True)


@pytest.mark.parametrize("chemin", ["note.txt", "script.py", ".obsidian/workspace.json", "workspace.md", "Projets"])
def test_ecriture_limitee_aux_notes_md(chemin):
    with pytest.raises(ErreurCoffre):
        normaliser(chemin, pour_ecrire=True)


def test_lister_cache_les_dossiers_caches(coffre):
    assert coffre.lister() == ["Projets/", "_index.md"]
    assert coffre.lister(recursif=True) == ["_index.md", "Projets/Projet MCP.md"]


def test_lire(coffre):
    assert "Serveur maison" in coffre.lire("Projets/Projet MCP.md")
    with pytest.raises(ErreurCoffre):
        coffre.lire(".obsidian/app.json")
    with pytest.raises(ErreurCoffre):
        coffre.lire("absente.md")


def test_lien_symbolique_vers_l_exterieur_refuse(coffre, tmp_path_factory):
    dehors = tmp_path_factory.mktemp("dehors")
    (dehors / "secret.md").write_text("secret")
    (coffre.racine / "lien.md").symlink_to(dehors / "secret.md")
    with pytest.raises(ErreurCoffre):
        coffre.lire("lien.md")


async def test_chercher(coffre):
    res = await coffre.chercher("oauth")
    assert [(r.chemin, r.ligne) for r in res] == [("Projets/Projet MCP.md", 3)]
    assert await coffre.chercher("app.json") == []  # .obsidian jamais fouillé


async def test_ecrire_creer_puis_refuser_l_ecrasement(coffre):
    assert "créée" in await coffre.ecrire_note("Journal/2026-11-26.md", "# Jour\n")
    assert (coffre.racine / "Journal" / "2026-11-26.md").read_text() == "# Jour\n"
    with pytest.raises(ErreurCoffre, match="existe déjà"):
        await coffre.ecrire_note("Journal/2026-11-26.md", "autre")
    assert "réécrite" in await coffre.ecrire_note("Journal/2026-11-26.md", "autre", remplacer_si_existe=True)


async def test_ajouter_et_remplacer(coffre):
    await coffre.ajouter_a_note("_index.md", "- nouvelle ligne")
    assert coffre.lire("_index.md") == "Index du coffre\n- nouvelle ligne\n"
    await coffre.remplacer_dans_note("Projets/Projet MCP.md", "OAuth à faire", "OAuth fait")
    assert "OAuth fait" in coffre.lire("Projets/Projet MCP.md")
    with pytest.raises(ErreurCoffre, match="introuvable"):
        await coffre.remplacer_dans_note("Projets/Projet MCP.md", "n'existe pas", "x")


async def test_remplacement_ambigu_refuse(coffre):
    await coffre.ecrire_note("double.md", "a\na\n")
    with pytest.raises(ErreurCoffre, match="2 fois"):
        await coffre.remplacer_dans_note("double.md", "a", "b")


async def test_conflit_detecte_par_etag(coffre):
    ecrivain = coffre.ecrivain
    contenu, etag = await ecrivain.lire("_index.md")
    await ecrivain.ecrire("_index.md", "modifié sur le Mac\n", etag=etag, creation=False)  # quelqu'un d'autre écrit
    with pytest.raises(ErreurCoffre, match="modifiée entre-temps"):
        await ecrivain.ecrire("_index.md", contenu + "ajout\n", etag=etag, creation=False)


async def test_conflit_resolu_en_relisant_sans_ecraser(coffre, monkeypatch):
    """Quelqu'un modifie la note pendant que Claude écrit : on relit, on réapplique, rien n'est perdu."""
    monkeypatch.setattr("coffre_mcp.coffre.asyncio.sleep", lambda s: _rien())
    ecrivain, ecrire_vrai, deja = coffre.ecrivain, coffre.ecrivain.ecrire, []

    async def ecrire_avec_concurrent(chemin, contenu, *, etag, creation):
        if not deja:
            deja.append(1)
            await ecrire_vrai(chemin, "Index du coffre\nligne ajoutée sur le Mac\n", etag=etag, creation=False)
        await ecrire_vrai(chemin, contenu, etag=etag, creation=creation)

    ecrivain.ecrire = ecrire_avec_concurrent
    await coffre.ajouter_a_note("_index.md", "- ajout de Claude")
    assert coffre.lire("_index.md") == "Index du coffre\nligne ajoutée sur le Mac\n- ajout de Claude\n"


async def _rien():
    return None



@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep absent (présent dans l'image Docker et la CI)")
async def test_ripgrep_et_recherche_python_donnent_le_meme_resultat(coffre):
    attendu = await coffre.chercher("oauth")          # fixture : recherche Python
    coffre._rg = shutil.which("rg")
    assert await coffre.chercher("oauth") == attendu
    assert await coffre.chercher("app.json") == []    # .obsidian jamais fouillé, même par ripgrep
