# Sonny Agent Control

[![Licença MIT](https://img.shields.io/badge/licença-MIT-green.svg)](LICENSE)

Orquestrador de agentes de IA para trabalhar com segurança em repositórios de código.

Você descreve uma tarefa, escolhe o projeto e o agente, e acompanha a execução pela interface web. Os agentes
podem ler e alterar o código em um worktree Git isolado, delegar partes do trabalho entre si e abrir pull
requests; as ações sensíveis esperam a aprovação de uma pessoa.

Feito com Django, Celery e LangGraph. Cada agente usa o provedor e o modelo do próprio cadastro
(Anthropic, OpenAI, Google ou Ollama).

## Recursos

- Projetos com repositório Git, branch padrão e chave SSH própria.
- Agentes configuráveis por provedor, modelo, prompt, ferramentas e permissões de delegação.
- Execuções assíncronas com histórico de eventos, delegação e consumo de tokens.
- Worktree isolado para cada execução, com branches `sonny/run-<id>`.
- Aprovação humana para comandos no shell e abertura de pull requests.
- Continuação de execuções interrompidas, cancelamento e detecção de workers inativos.

## Requisitos

- Docker com o plugin Compose

Para rodar fora do Docker (opcional): Python 3.14 com [uv](https://docs.astral.sh/uv/) e Node.js.

## Subindo com Docker

Os ambientes são montados em camadas: `compose.yaml` descreve os serviços, e `compose.dev.yaml` ou
`compose.prod.yaml` acrescentam o que é próprio de cada ambiente. O `.env` escolhe qual vale, pela
variável `COMPOSE_FILE`.

| Serviço | Papel |
|---|---|
| `web` | Interface web; aplica as migrações ao subir |
| `worker` | Executa os agentes (Celery) |
| `beat` | Agenda a limpeza diária dos worktrees antigos |
| `postgres`, `redis` | Banco e fila |
| `tailwind`, `mailpit` | Só em desenvolvimento: recompila o CSS e recebe os e-mails |

### Desenvolvimento

```bash
cp .env.example .env     # a chave do provedor de LLM pode ir aqui ou na tela de Integrações
docker compose up        # interface em http://localhost:8000
```

O código entra nos containers por bind mount: a web recarrega sozinha, o worker reinicia quando um `.py`
muda e o CSS é recompilado quando um template muda. Crie sua conta em `/accounts/signup/`; os e-mails caem
no Mailpit, em http://localhost:8025.

```bash
docker compose exec web python manage.py test                   # testes
docker compose exec web python manage.py cleanup_workspaces     # limpeza manual dos worktrees
docker compose build                                            # depois de mudar dependências
```

### Produção

No `.env` do servidor, troque `COMPOSE_FILE` para `compose.yaml:compose.prod.yaml` e defina `SECRET_KEY`,
`POSTGRES_PASSWORD`, `ALLOWED_HOSTS` e `CSRF_TRUSTED_ORIGINS` (o `.env.example` lista todos). Depois:

```bash
docker compose up -d --build
docker compose exec web python manage.py createsuperuser    # a criação de conta pela tela fica fechada
```

O código e os arquivos estáticos ficam dentro da imagem e a web roda com gunicorn. Ela escuta só em
`127.0.0.1:8000`: coloque um proxy com HTTPS (Caddy, nginx, Traefik) na frente, repassando o cabeçalho
`X-Forwarded-Proto`. Para atualizar, `git pull` e `docker compose up -d --build`.

### Rodando fora do Docker

Dá para deixar só o banco, a fila e o Mailpit no Docker e rodar o resto na máquina:

```bash
docker compose up -d postgres redis mailpit
uv sync && npm install && npm run build
uv run django_tabler_icons download --yes   # ícones da interface, baixados uma vez por máquina
uv run python manage.py migrate
uv run python manage.py runserver        # interface web
uv run celery -A config worker -l info   # executa os agentes (outro terminal)
```

## Configuração

Quase tudo vem de variáveis de ambiente, lidas do `.env`. As principais:

| Variável | Para que serve |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` | Chave de cada provedor de LLM usado pelos agentes |
| `GITHUB_TOKEN` | Opcional: abrir pull requests pela API do GitHub (veja "Acesso aos repositórios") |
| `DATABASE_URL`, `CELERY_BROKER_URL` | Postgres e Redis (o padrão aponta para o `docker compose`) |
| `CELERY_WORKER_CONCURRENCY` | Quantas execuções rodam ao mesmo tempo (padrão 2) |
| `RUN_MAX_TOKENS` | Teto de tokens por execução, somando todos os agentes; 0 desliga |
| `WORKSPACE_RETENTION_DAYS` | Dias que o worktree de uma execução finalizada fica guardado |

As chaves dos provedores e o token do GitHub também podem ser cadastrados na tela **Integrações** (menu do
usuário). A chave salva lá fica no banco, criptografada com a `SECRET_KEY`, vale antes da variável de ambiente e
entra em uso na execução seguinte, sem reiniciar nada. Depois de salva, a tela mostra só o final dela; se a
`SECRET_KEY` mudar, cadastre as chaves de novo.

Os demais limites e prazos de operação (tempo máximo de um comando, tamanho da saída das ferramentas, passos
por execução etc.) estão reunidos na seção "Orquestrador" do `config/settings.py`, e cada um aceita uma variável
de ambiente de mesmo nome.

### Acesso aos repositórios

Cada projeto tem um par de chaves SSH próprio, gerado pelo sistema. A tela do projeto mostra a chave pública:
cadastre-a no repositório (no GitHub, em *Settings → Deploy keys*, com *Allow write access*) e use o endereço
SSH (`git@github.com:organizacao/repositorio.git`) no projeto. O botão "Testar acesso" confere a leitura.
A chave privada fica no banco, criptografada com a `SECRET_KEY`: se a `SECRET_KEY` mudar, gere chaves novas.

O token do GitHub é opcional. Com ele, na tela de Integrações ou no `GITHUB_TOKEN`, os agentes abrem o pull
request sozinhos. Só a variável `GITHUB_TOKEN` faz os endereços `https` do GitHub funcionarem em repositórios
privados, porque ela é aplicada ao git quando o container sobe. Sem token, o agente envia a branch e entrega
o link para você abrir o pull request.

## Como funciona

- **Projeto**: um repositório git e a branch de onde as execuções partem.
- **Agente**: provedor, modelo, instruções, ferramentas habilitadas e para quais agentes ele pode delegar.
- **Skill**: instruções reutilizáveis em Markdown (convenções do projeto, roteiro de revisão). Escritas uma
  vez e marcadas em vários agentes, entram nas instruções deles em toda execução.
- **Execução**: uma tarefa dada a um agente em um projeto. Roda no worker, em um worktree próprio
  (`.workspaces/runs/<id>`, branch `sonny/run-<id>`), com o estado salvo no Postgres a cada passo.
- **Aprovação**: rodar comando e abrir pull request pausam a execução até alguém aprovar ou negar.
- **Modo autônomo**: marcado ao enviar a tarefa, dispensa as aprovações daquela execução. O agente roda
  comandos e abre o pull request sem parar; nada é mesclado sozinho.

Uma execução pode ser cancelada, continuada do último passo salvo (se falhou) ou repetida do zero. Depois de
concluída, a aba "Alterações" mostra o diff e você pode pedir um ajuste: o agente continua a mesma conversa, no
mesmo worktree.

> **Atenção:** a ferramenta "Rodar comando" executa no worktree, mas com o usuário e as permissões do
> worker. Um comando aprovado alcança a máquina toda; a aprovação é a única barreira, e o modo autônomo
> a remove. Use-o só com agentes e repositórios em que você confia.

## Estrutura

```
Dockerfile, compose*.yaml, docker/   imagem e ambientes de desenvolvimento e produção
config/                              settings, urls e a configuração do Celery
orchestrator/                        o app Django
    models.py                        Project, Agent, Run, RunEvent, Approval
    views.py, forms.py, urls.py      telas
    tasks.py                         tarefas do Celery: executar uma execução, limpar worktrees
    graph.py                         grafo LangGraph: laço do agente, aprovação e delegação
    tools.py                         ferramentas dos agentes
    workspace.py                     clones e worktrees git
    tests/                           testes, um arquivo por assunto
templates/                           telas (Flowbite + Tailwind, htmx e Alpine)
static/                              CSS de entrada e JavaScript de terceiros
```

## Testes e manutenção

```bash
docker compose exec web python manage.py test                          # ou: uv run python manage.py test
docker compose exec web python manage.py cleanup_workspaces --days 7   # apaga worktrees de execuções antigas
```

## Licença

Distribuído sob a [licença MIT](LICENSE).
