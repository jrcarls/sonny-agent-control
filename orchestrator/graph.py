"""Grafo de uma execução: o agente escolhido trabalha e pode delegar tarefas a outros agentes.

Só um agente trabalha por vez. Quem delega fica esperando numa pilha (`frames`) até o outro
terminar; a resposta volta para ele como resultado da ferramenta de delegação.
"""

import json
from typing import TypedDict

from langchain.chat_models import init_chat_model
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .models import RunEvent
from .providers import MODEL_PROVIDERS, chat_model_options
from .tools import ASK_TOOL, TOOL_SPECS, build_tools

DELEGATE_TOOL = "delegate_task"
# Entrada do log que não vira evento: consumo de tokens de uma chamada ao modelo
USAGE = "usage"


# O que o agente recebe quando pergunta e ninguém responde
NO_ANSWER = "Siga com a opção mais segura e diga na resposta final o que você supôs."

# Quantas vezes o agente principal é chamado de novo quando o modelo devolve uma resposta vazia
EMPTY_RESPONSE_RETRIES = 1


class EmptyResponseError(Exception):
    """O modelo do agente principal não devolveu texto nem pedido de ferramenta."""


class Frame(TypedDict):
    """Um agente em atividade: o do topo da pilha trabalha, os de baixo esperam por ele."""

    agent_id: int
    messages: list
    call_id: str | None  # chamada de delegação, na conversa de quem delegou, que espera esta resposta
    queue: list  # delegações pedidas por este agente que ainda não começaram


class State(TypedDict, total=False):
    frames: list[Frame]
    memory: dict  # id do agente (str) -> conversa dele nesta execução, retomada se for chamado de novo
    # id da chamada de ferramenta -> True/False (aprovação) ou o texto da resposta (pergunta ao usuário),
    # preenchido pelo nó de aprovação
    decisions: dict
    log: list  # eventos do passo atual, gravados pela tarefa como RunEvent


def build_chat_model(agent):
    """Cria o modelo do agente, com a chave de Integrações ou, sem ela, a variável de ambiente do provedor."""
    options = chat_model_options(agent.provider)
    return init_chat_model(agent.model, model_provider=MODEL_PROVIDERS[agent.provider], **options)


def initial_state(run):
    frame = Frame(agent_id=run.agent_id, messages=[HumanMessage(run.prompt)], call_id=None, queue=[])
    return {"frames": [frame], "memory": {}, "decisions": {}}


def follow_up_state(values, message):
    """Estado para continuar uma execução que já terminou: a mesma conversa, com o ajuste pedido no fim."""
    frames = _copy(values["frames"])
    frames[-1]["messages"].append(HumanMessage(message))
    return {"frames": frames}


def _load_agents(lead):
    """Agentes que podem participar: o escolhido e todos os alcançáveis por delegação (só os ativos)."""
    agents, delegates, pending = {lead.pk: lead}, {}, [lead]
    while pending:
        agent = pending.pop()
        delegates[agent.pk] = list(agent.delegates.filter(is_active=True).exclude(pk=agent.pk))
        for other in delegates[agent.pk]:
            if other.pk not in agents:
                agents[other.pk] = other
                pending.append(other)
    return agents, delegates


def _delegate_tool(delegates):
    team = "\n".join(f"- {agent.name}: {agent.description or 'sem descrição'}" for agent in delegates)

    @tool(DELEGATE_TOOL, description=f"Passa uma tarefa a outro agente e espera a resposta dele.\nAgentes:\n{team}")
    def delegate_task(agent: str, task: str) -> str:
        raise NotImplementedError  # nunca é chamada: a delegação é tratada pelo grafo

    return delegate_task


def _system_prompt(run, agent, skills, delegates, caller):
    project = run.project
    parts = [
        agent.system_prompt,
        *(f"## Skill: {skill.name}\n\n{skill.content}" for skill in skills),
        f"Projeto: {project.name}\n"
        f"Repositório: {project.repo_url}\n"
        f"Você trabalha em uma cópia isolada do repositório, na branch {run.branch}, "
        f"criada a partir de {project.default_branch}. "
        "Os caminhos das ferramentas são relativos à raiz do repositório.",
    ]
    if delegates:
        parts.append(
            f"Você pode delegar partes do trabalho com a ferramenta {DELEGATE_TOOL}; "
            "o outro agente trabalha no mesmo repositório e você recebe a resposta dele."
        )
    if caller:
        parts.append(f"Você foi chamado pelo agente {caller.name}. Sua resposta final é entregue a ele.")
    return "\n\n".join(part for part in parts if part)


def _copy(frames):
    return [{**frame, "messages": list(frame["messages"]), "queue": list(frame["queue"])} for frame in frames]


def _entry(kind, agent, content):
    return {"kind": kind, "agent_id": agent.pk, "content": content}


def build_graph(run, workspace, checkpointer):
    agents, delegates = _load_agents(run.agent)
    skills = {pk: list(agent.skills.all()) for pk, agent in agents.items()}
    toolsets = {pk: {item.name: item for item in build_tools(run, workspace, agent)} for pk, agent in agents.items()}
    models = {}

    def model_for(agent):
        if agent.pk not in models:
            bound = list(toolsets[agent.pk].values())
            if delegates[agent.pk]:
                bound.append(_delegate_tool(delegates[agent.pk]))
            model = build_chat_model(agent)
            models[agent.pk] = model.bind_tools(bound) if bound else model
        return models[agent.pk]

    def needs_approval(agent_id, call):
        # No modo autônomo ninguém é consultado. Fora dele, ferramenta que o agente não tem habilitada
        # é recusada direto, sem incomodar ninguém.
        if run.autonomous:
            return False
        return call["name"] in toolsets[agent_id] and TOOL_SPECS[call["name"]].requires_approval

    def waits_for_user(agent_id, call):
        """A chamada pausa a execução: precisa de aprovação ou é uma pergunta para a pessoa."""
        if needs_approval(agent_id, call):
            return True
        return call["name"] == ASK_TOOL and ASK_TOOL in toolsets[agent_id] and not run.autonomous

    def work(state: State):
        """O agente do topo da pilha dá um passo: responde ou pede ferramentas."""
        frames = _copy(state["frames"])
        frame = frames[-1]
        agent = agents[frame["agent_id"]]
        caller = agents[frames[-2]["agent_id"]] if len(frames) > 1 else None
        system = SystemMessage(_system_prompt(run, agent, skills[agent.pk], delegates[agent.pk], caller))
        usages = []
        for _ in range(1 + EMPTY_RESPONSE_RETRIES):
            response = model_for(agent).invoke([system, *frame["messages"]])
            usages.append(response.usage_metadata or {})
            # Alguns modelos às vezes devolvem uma resposta sem nada. No agente principal isso encerraria
            # a execução como concluída e sem resultado, então ele é chamado de novo.
            if response.text or response.tool_calls or caller:
                break
        else:
            raise EmptyResponseError(
                "o modelo devolveu uma resposta vazia mais de uma vez. Use “Continuar de onde parou” para tentar de novo."
            )
        frame["messages"].append(response)

        log = []
        if response.text:
            log.append(_entry(RunEvent.Kind.MESSAGE, agent, str(response.text)))
        for call in response.tool_calls:
            if call["name"] != DELEGATE_TOOL:
                arguments = json.dumps(call["args"], ensure_ascii=False, indent=2)
                log.append(_entry(RunEvent.Kind.TOOL_CALL, agent, f"{call['name']}\n{arguments}"))
        # Por último: se o limite de tokens estourar, a resposta do agente já ficou registrada
        for usage in usages:
            log.append({"kind": USAGE, "input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)})
        return {"frames": frames, "log": log}

    def after_work(state: State):
        frames = state["frames"]
        calls = frames[-1]["messages"][-1].tool_calls
        if not calls:
            return "handoff" if len(frames) > 1 else END
        agent_id = frames[-1]["agent_id"]
        return "approval" if any(waits_for_user(agent_id, call) for call in calls) else "tools"

    def approval(state: State):
        # Este nó só pausa o grafo. Ao retomar ele roda de novo do início, por isso não tem efeito colateral.
        frame = state["frames"][-1]
        agent_id = frame["agent_id"]
        requested = [
            {"id": call["id"], "name": call["name"], "args": call["args"], "agent_id": agent_id}
            for call in frame["messages"][-1].tool_calls
            if waits_for_user(agent_id, call)
        ]
        return {"decisions": interrupt(requested), "log": []}

    def run_tools(state: State):
        """Executa as ferramentas pedidas pelo agente do topo e põe as delegações na fila dele."""
        decisions = state.get("decisions") or {}
        frames = _copy(state["frames"])
        frame = frames[-1]
        agent = agents[frame["agent_id"]]
        tools = toolsets[agent.pk]
        team = {other.name: other for other in delegates[agent.pk]}
        waiting = {item["agent_id"] for item in frames}
        log = []

        for call in frame["messages"][-1].tool_calls:
            name, args = call["name"], call["args"]
            if name == DELEGATE_TOOL and team:
                target = team.get(args.get("agent"))
                if target is None:
                    content, status = f"Agente indisponível: {args.get('agent')}. Opções: {', '.join(team)}.", "error"
                elif target.pk in waiting:
                    content, status = f"{target.name} está esperando esta tarefa terminar e não pode recebê-la.", "error"
                else:
                    frame["queue"].append({"id": call["id"], "agent_id": target.pk, "task": str(args.get("task", ""))})
                    continue
            elif name not in tools:
                content, status = f"Ferramenta indisponível: {name}", "error"
            elif name == ASK_TOOL:
                answer = decisions.get(call["id"])
                if run.autonomous:
                    content = f"Modo autônomo: não há ninguém para responder. {NO_ANSWER}"
                elif isinstance(answer, str) and answer.strip():
                    content = f"Resposta do usuário:\n{answer.strip()}"
                else:
                    content = f"O usuário preferiu não responder. {NO_ANSWER}"
                status = "success"
            elif needs_approval(agent.pk, call) and not decisions.get(call["id"]):
                content, status = "Ação negada pelo usuário.", "error"
            else:
                try:
                    content, status = str(tools[name].invoke(args)), "success"
                except Exception as exc:  # o erro volta para o agente decidir o que fazer
                    content, status = f"Erro: {exc}", "error"
            frame["messages"].append(ToolMessage(content=content, tool_call_id=call["id"], name=name, status=status))
            # Delegação recusada não tem chamada de ferramenta registrada: entra como evento de delegação
            kind = RunEvent.Kind.DELEGATION if name == DELEGATE_TOOL else RunEvent.Kind.TOOL_RESULT
            log.append(_entry(kind, agent, content))
        return {"frames": frames, "decisions": {}, "log": log}

    def delegate(state: State):
        """Começa a próxima delegação da fila: o agente chamado vai para o topo da pilha."""
        frames = _copy(state["frames"])
        frame = frames[-1]
        item = frame["queue"].pop(0)
        caller, target = agents[frame["agent_id"]], agents[item["agent_id"]]
        history = (state.get("memory") or {}).get(str(target.pk), [])
        task = HumanMessage(f"Tarefa recebida de {caller.name}:\n{item['task']}")
        frames.append(Frame(agent_id=target.pk, messages=[*history, task], call_id=item["id"], queue=[]))
        log = [_entry(RunEvent.Kind.DELEGATION, caller, f"{caller.name} pediu a {target.name}:\n{item['task']}")]
        return {"frames": frames, "log": log}

    def handoff(state: State):
        """O agente do topo terminou: sai da pilha e a resposta dele vai para quem delegou."""
        frames = _copy(state["frames"])
        done = frames.pop()
        frame = frames[-1]
        agent, caller = agents[done["agent_id"]], agents[frame["agent_id"]]
        answer = str(done["messages"][-1].text) or "(sem resposta)"
        frame["messages"].append(ToolMessage(content=answer, tool_call_id=done["call_id"], name=DELEGATE_TOOL))
        memory = {**(state.get("memory") or {}), str(agent.pk): done["messages"]}
        log = [_entry(RunEvent.Kind.DELEGATION, agent, f"{agent.name} entregou a resposta a {caller.name}.")]
        return {"frames": frames, "memory": memory, "log": log}

    def next_step(state: State):
        return "delegate" if state["frames"][-1]["queue"] else "work"

    builder = StateGraph(State)
    builder.add_node("work", work)
    builder.add_node("approval", approval)
    builder.add_node("tools", run_tools)
    builder.add_node("delegate", delegate)
    builder.add_node("handoff", handoff)
    builder.add_edge(START, "work")
    builder.add_conditional_edges("work", after_work, ["approval", "tools", "handoff", END])
    builder.add_edge("approval", "tools")
    builder.add_conditional_edges("tools", next_step, ["delegate", "work"])
    builder.add_conditional_edges("handoff", next_step, ["delegate", "work"])
    builder.add_edge("delegate", "work")
    return builder.compile(checkpointer=checkpointer)
