import httpx

from coffre_mcp.webdav import ClientWebDAV


async def test_etag_sans_suffixe_gzip_et_lecture_sans_compression():
    vus = {}

    def nextcloud(requete: httpx.Request) -> httpx.Response:
        if requete.method == "GET":
            vus["accept-encoding"] = requete.headers.get("accept-encoding")
            return httpx.Response(200, text="contenu", headers={"ETag": '"abc123-gzip"'})
        vus["if-match"] = requete.headers.get("if-match")
        return httpx.Response(204)

    c = ClientWebDAV("http://nc", "claude-mcp", "x", "Shared/Notes", transport=httpx.MockTransport(nextcloud))
    contenu, etag = await c.lire("_index.md")
    assert (contenu, etag) == ("contenu", '"abc123"')
    assert vus["accept-encoding"] == "identity"
    await c.ecrire("_index.md", "nouveau", etag=etag, creation=False)
    assert vus["if-match"] == '"abc123"'
