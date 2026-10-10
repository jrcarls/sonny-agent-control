"""Chaves SSH dos projetos: geração do par e proteção da parte privada guardada no banco."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.conf import settings


class DecryptionError(Exception):
    pass


def _fernet():
    # Chave derivada da SECRET_KEY: quem só tem o banco não consegue ler as chaves privadas
    digest = hashlib.sha256(b"orchestrator.ssh-keys:" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(text):
    return _fernet().encrypt(text.encode()).decode()


def decrypt(token):
    try:
        return _fernet().decrypt(token.encode()).decode()
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
