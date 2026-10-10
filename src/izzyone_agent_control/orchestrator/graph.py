from langchain.chat_models import init_chat_model
from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from .models import Agent

# Nome do provedor no cadastro do agente -> nome esperado pelo init_chat_model
MODEL_PROVIDERS = {
    Agent.Provider.ANTHROPIC: "anthropic",
    Agent.Provider.OPENAI: "openai",
    Agent.Provider.GOOGLE: "google_genai",
    Agent.Provider.OLLAMA: "ollama",
}


def build_chat_model(agent):
    """Cria o modelo do agente. As chaves de API vêm das variáveis de ambiente de cada provedor."""
    return init_chat_model(agent.model, model_provider=MODEL_PROVIDERS[agent.provider])


def system_prompt(run):
    project = run.project
    context = f"Projeto: {project.name}\nRepositório: {project.repo_url} (branch {project.default_branch})"
    return "\n\n".join(part for part in [run.agent.system_prompt, context] if part)


def build_graph(run, checkpointer):
    """Grafo de um agente só: recebe a tarefa e responde."""
    model = build_chat_model(run.agent)
    system = SystemMessage(system_prompt(run))

    def agent(state: MessagesState):
        return {"messages": [model.invoke([system, *state["messages"]])]}

    builder = StateGraph(MessagesState)
    builder.add_node("agent", agent)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    return builder.compile(checkpointer=checkpointer)
