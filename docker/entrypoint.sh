#!/bin/sh
set -e

# Com GITHUB_TOKEN definido, o git usa o token para clonar e enviar para qualquer repositório
# do GitHub, tanto por endereço https quanto por endereço ssh, sem precisar de chave SSH.
if [ -n "$GITHUB_TOKEN" ]; then
    authenticated="https://x-access-token:${GITHUB_TOKEN}@github.com/"
    git config --global --replace-all url."$authenticated".insteadOf "https://github.com/"
    git config --global --add url."$authenticated".insteadOf "git@github.com:"
    git config --global --add url."$authenticated".insteadOf "ssh://git@github.com/"
fi

exec "$@"
