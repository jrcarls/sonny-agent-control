from django import template

register = template.Library()


@register.filter
def duration(value):
    """Um intervalo de tempo por extenso e curto: "42 s", "3 min 05 s", "1 h 12 min". Uso: {{ run.duration|duration }}"""
    if value is None:
        return ""
    seconds = max(int(value.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds:02d} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes:02d} min"
