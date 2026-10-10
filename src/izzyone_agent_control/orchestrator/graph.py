from langchain.chat_models import init_chat_model
from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import interrupt

from .models import Agent
from .tools import TOOL_SPECS, build_tools

# Nome do provedor no cadastro do agente -> nome esperado pelo init_chat_model
MODEL_PROVIDERS = {
    Agent.Provider.ANTHROPIC: "anthropic",
    Agent.Provider.OPENAI: "openai",
    Agent.Provider.GOOGLE: "google_genai",
    Agent.Provider.OLLAMA: "ollama",
}


class State(MessagesState):
    # id da chamada de ferramenta -> True/False, preenchido pelo nó de aprovação
    decisions: dict


def build_chat_model(agent):
    """Cria o modelo do agente. As chaves de API vêm das variáveis de ambiente de cada provedor."""
    return init_chat_model(agent.model, model_provider=MODEL_PROVIDERS[agent.provider])


def system_prompt(run):
    project = run.project
    context = (
        f"Projeto: {project.name}\n"
        f"Repositório: {project.repo_url}\n"
        f"Você trabalha em uma cópia isolada do repositório, na branch {run.branch}, "
        f"criada a partir de {project.default_branch}. "
        "Os caminhos das ferramentas são relativos à raiz do repositório."
    )
    return "\n\n".join(part for part in [run.agent.system_prompt, context] if part)


def build_graph(run, workspace, checkpointer):
    """Laço do agente: o modelo responde ou pede ferramentas; as sensíveis esperam aprovação humana."""
    tools = {item.name: item for item in build_tools(run, workspace)}
    model = build_chat_model(run.agent)
    if tools:
        model = model.bind_tools(list(tools.values()))
    system = SystemMessage(system_prompt(run))

    def _needs_approval(call):
        # Ferramenta que o agente não tem habilitada é recusada direto, sem incomodar ninguém
        return call["name"] in tools and TOOL_SPECS[call["name"]].requires_approval

    def agent(state: State):
        return {"messages": [model.invoke([system, *state["messages"]])]}

    def route(state: State):
        calls = state["messages"][-1].tool_calls
        if not calls:
            return END
        return "approval" if any(_needs_approval(call) for call in calls) else "tools"

    def approval(state: State):
        # Este nó só pausa o grafo. Ao retomar ele roda de novo do início, por isso não tem efeito colateral.
        calls = [call for call in state["messages"][-1].tool_calls if _needs_approval(call)]
        decisions = interrupt([{"id": call["id"], "name": call["name"], "args": call["args"]} for call in calls])
        return {"decisions": decisions}

    def run_tools(state: State):
        decisions = state.get("decisions") or {}
        messages = []
        for call in state["messages"][-1].tool_calls:
            if call["name"] not in tools:
                content, status = f"Ferramenta indisponível: {call['name']}", "error"
            elif _needs_approval(call) and not decisions.get(call["id"]):
                content, status = "Ação negada pelo usuário.", "error"
            else:
                try:
                    content, status = str(tools[call["name"]].invoke(call["args"])), "success"
                except Exception as exc:  # o erro volta para o agente decidir o que fazer
                    content, status = f"Erro: {exc}", "error"
            messages.append(ToolMessage(content=content, tool_call_id=call["id"], name=call["name"], status=status))
        return {"messages": messages, "decisions": {}}

    builder = StateGraph(State)
    builder.add_node("agent", agent)
    builder.add_node("approval", approval)
    builder.add_node("tools", run_tools)
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, ["approval", "tools", END])
    builder.add_edge("approval", "tools")
    builder.add_edge("tools", "agent")
    return builder.compile(checkpointer=checkpointer)
