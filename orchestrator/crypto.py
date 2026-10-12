"""Segredos guardados no banco: as chaves SSH dos projetos (e a geração do par) e as chaves de API."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings


# Para que o segredo serve. Cada uso tem a sua chave de criptografia: o que foi cifrado para um não abre no outro.
SSH_KEYS = "ssh-keys"
API_KEYS = "api-keys"


class DecryptionError(Exception):
    pass


def _fernet(purpose):
    # Chave derivada da SECRET_KEY: quem só tem o banco não consegue ler os segredos
    digest = hashlib.sha256(f"orchestrator.{purpose}:".encode() + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text, purpose=SSH_KEYS):
    return _fernet(purpose).encrypt(text.encode()).decode()


def decrypt(token, purpose=SSH_KEYS):
    try:
        return _fernet(purpose).decrypt(token.encode()).decode()
    except InvalidToken:
        raise DecryptionError("o valor não pôde ser lido com a SECRET_KEY atual") from None


def generate_ssh_keypair(comment):
    """Par de chaves Ed25519 no formato do OpenSSH: (privada, pública)."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()
    ).decode()
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    return private, f"{public} {comment}"
