"""Génère les secrets du serveur MCP et les écrit dans le .env — à lancer par le propriétaire du coffre, dans son terminal
(les secrets ne passent jamais par un tiers).

    docker compose run --rm -it coffre-mcp python -m coffre_mcp.init_secrets /app/.env

- secret du client OAuth (à recopier dans claude.ai et dans un gestionnaire de mots de passe) ;
- empreinte de la phrase de passe (la phrase elle-même n'est écrite nulle part) ;
- secret TOTP, affiché en QR code à scanner avec l'appli d'authentification.
Les autres lignes du .env (mot de passe d'application Nextcloud…) sont conservées.
"""

import getpass
import os
import secrets
import sys
from pathlib import Path

import pyotp
import qrcode

from .auth import hacher_phrase


def main() -> None:
    fichier = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
    lignes = fichier.read_text().splitlines() if fichier.exists() else []
    gardees = [l for l in lignes if not l.split("=", 1)[0] in
               {"OAUTH_CLIENT_ID", "OAUTH_CLIENT_SECRET", "LOGIN_PASSPHRASE_HASH", "TOTP_SECRET"}]

    print("Phrase de passe de connexion (longue : 4 mots ou plus). Elle ne s'affiche pas.")
    while True:
        p1 = getpass.getpass("Phrase de passe : ")
        if len(p1) < 16:
            print("Trop courte (16 caractères minimum)."); continue
        if getpass.getpass("Encore une fois : ") == p1:
            break
        print("Les deux saisies diffèrent.")
    print("Calcul de l'empreinte…")
    empreinte = hacher_phrase(p1)

    totp_secret = pyotp.random_base32()
    uri = pyotp.TOTP(totp_secret).provisioning_uri(name=os.environ.get("TOTP_NOM", "coffre"), issuer_name="Coffre MCP")
    qr = qrcode.QRCode(border=1); qr.add_data(uri); qr.make()
    print("\nScanne ce QR code avec ton appli d'authentification (Aegis, 2FAS, gestionnaire de mots de passe…) :\n")
    qr.print_ascii(invert=True)
    print(f"\n(ou saisis la clé à la main : {totp_secret})\n")
    while not pyotp.TOTP(totp_secret).verify(input("Code à 6 chiffres affiché par l'appli : ").strip(), valid_window=1):
        print("Code incorrect, réessaie.")

    client_id, client_secret = "claude-ai", secrets.token_urlsafe(32)
    gardees += [f"OAUTH_CLIENT_ID='{client_id}'", f"OAUTH_CLIENT_SECRET='{client_secret}'",
                f"LOGIN_PASSPHRASE_HASH='{empreinte}'", f"TOTP_SECRET='{totp_secret}'"]
    fichier.write_text("\n".join(gardees) + "\n")
    print(f"\n.env mis à jour. À recopier dans claude.ai (et dans ton gestionnaire de mots de passe) :\n"
          f"  Client ID     : {client_id}\n  Client Secret : {client_secret}\n")


if __name__ == "__main__":
    main()
