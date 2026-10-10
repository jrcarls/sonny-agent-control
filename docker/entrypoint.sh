#!/bin/sh
set -e

# Endereços SSH (git@…) usam a chave do próprio projeto, cadastrada no repositório.
# Com GITHUB_TOKEN definido, os endereços https do GitHub passam a usar o token.
if [ -n "$GITHUB_TOKEN" ]; then
    git config --global --replace-all \
        url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/"
fi

exec "$@"
