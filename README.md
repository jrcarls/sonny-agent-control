# IzzyOne Agentes Control

Orquestrador de agentes de IA que trabalham em repositórios de código. Você descreve uma tarefa, escolhe o
projeto e o agente, e acompanha a execução pela interface web. Os agentes podem ler e alterar o código em um
worktree git isolado, delegar partes do trabalho entre si e abrir pull requests; as ações sensíveis esperam
a aprovação de uma pessoa.

Feito com Django, Celery e LangGraph. Cada agente usa o provedor e o modelo do próprio cadastro
(Anthropic, OpenAI, Google ou Ollama).

## Requisitos

- Python 3.14 e [uv](https://docs.astral.sh/uv/)
- Node.js (só para compilar o CSS)
- Docker (Postgres, Redis e Mailpit de desenvolvimento)
- git, com credenciais para clonar e enviar para os repositórios dos projetos

## Primeira vez

```bash
cp .env.example .env            # preencha a chave do provedor de LLM que for usar
docker compose up -d            # Postgres, Redis e Mailpit
uv sync
npm install && npm run build
uv run python manage.py migrate
```

## Rodando

São dois processos obrigatórios e dois opcionais, cada um em um terminal:

```bash
uv run python manage.py runserver        # interface web, em http://localhost:8000
uv run celery -A config worker -l info   # executa os agentes
uv run celery -A config beat -l info     # opcional: limpeza diária dos worktrees antigos
npm run css:watch                        # opcional: recompila o CSS ao editar templates
```

Crie sua conta em `/accounts/signup/`; os e-mails de desenvolvimento caem no Mailpit, em
http://localhost:8025. Depois cadastre um projeto e um agente e envie uma tarefa pela tela inicial.

## Configuração

Tudo vem de variáveis de ambiente, lidas do `.env`. As principais:

| Variável | Para que serve |
|---|---|
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` | Chave de cada provedor de LLM usado pelos agentes |
| `GITHUB_TOKEN` | Abrir pull requests pela API do GitHub |
| `DATABASE_URL`, `CELERY_BROKER_URL` | Postgres e Redis (o padrão aponta para o `docker compose`) |
| `CELERY_WORKER_CONCURRENCY` | Quantas execuções rodam ao mesmo tempo (padrão 2) |
| `RUN_MAX_TOKENS` | Teto de tokens por execução, somando todos os agentes; 0 desliga |
| `WORKSPACE_RETENTION_DAYS` | Dias que o worktree de uma execução finalizada fica guardado |

O worker clona e envia código com as credenciais git da máquina em que roda (chave SSH ou credential helper).

## Como funciona

- **Projeto**: um repositório git e a branch de onde as execuções partem.
- **Agente**: provedor, modelo, instruções, ferramentas habilitadas e para quais agentes ele pode delegar.
- **Execução**: uma tarefa dada a um agente em um projeto. Roda no worker, em um worktree próprio
  (`.workspaces/runs/<id>`, branch `izzyone/run-<id>`), com o estado salvo no Postgres a cada passo.
- **Aprovação**: rodar comando e abrir pull request pausam a execução até alguém aprovar ou negar.

Uma execução pode ser cancelada, continuada do último passo salvo (se falhou) ou repetida do zero.

> **Atenção:** a ferramenta "Rodar comando" executa no worktree, mas com o usuário e as permissões do
> worker. Um comando aprovado alcança a máquina toda; a aprovação é a única barreira.

## Estrutura

```
config/                              settings, urls e a configuração do Celery
src/izzyone_agent_control/orchestrator/
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
uv run python manage.py test izzyone_agent_control    # precisa do Postgres no ar
uv run python manage.py cleanup_workspaces --days 7   # apaga worktrees de execuções antigas
```
