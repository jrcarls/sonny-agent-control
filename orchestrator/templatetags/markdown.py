from django import template
from django.utils.safestring import mark_safe
from markdown_it import MarkdownIt

register = template.Library()

# html=False: HTML que o agente escrever sai como texto, nunca como marcação. É isso que torna seguro
# marcar o resultado como confiável; a biblioteca também recusa links javascript: e semelhantes.
_markdown = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def _open_link_in_new_tab(renderer, tokens, idx, options, env):
    tokens[idx].attrSet("target", "_blank")
    tokens[idx].attrSet("rel", "noopener noreferrer nofollow")
    return renderer.renderToken(tokens, idx, options, env)


_markdown.add_render_rule("link_open", _open_link_in_new_tab)


@register.filter
def render_markdown(text):
    """Converte o Markdown das respostas dos agentes em HTML. Uso: {{ run.result|render_markdown }}"""
    return mark_safe(_markdown.render(text or ""))
