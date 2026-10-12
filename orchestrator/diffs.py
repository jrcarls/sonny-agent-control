"""Leitura do diff unificado do git, para a tela da execução mostrar o que mudou arquivo por arquivo."""

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class DiffLine:
    kind: str  # "add", "del" ou "context"
    text: str
    old: int | None = None
    new: int | None = None


@dataclass
class Hunk:
    header: str
    lines: list = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    status: str = "modified"  # "added", "deleted", "renamed" ou "modified"
    old_path: str = ""
    binary: bool = False
    # Passou do limite de linhas: as contagens continuam certas, mas as linhas não são guardadas
    too_large: bool = False
    additions: int = 0
    deletions: int = 0
    hunks: list = field(default_factory=list)


def _header_path(line):
    """Caminho em "diff --git a/<caminho> b/<caminho>". Só vale sem renomeação, quando os dois lados são iguais."""
    sides = line.removeprefix("diff --git ")
    half = (len(sides) - 1) // 2
    return sides[half + 1 :].removeprefix("b/")


def parse_diff(text, max_file_lines):
    """Arquivos de um diff do git, na ordem em que aparecem. Em arquivo com mais de max_file_lines só conta."""
    files, current, hunk, old, new, shown = [], None, None, 0, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            current, hunk, shown = FileDiff(path=_header_path(line)), None, 0
            files.append(current)
        elif current is None:
            continue
        elif hunk is None and not line.startswith("@@"):
            # Cabeçalho do arquivo, antes do primeiro trecho
            if line.startswith("new file mode"):
                current.status = "added"
            elif line.startswith("deleted file mode"):
                current.status = "deleted"
            elif line.startswith("rename from "):
                current.status, current.old_path = "renamed", line.removeprefix("rename from ")
            elif line.startswith("rename to "):
                current.path = line.removeprefix("rename to ")
            elif line.startswith("Binary files ") or line == "GIT binary patch":
                current.binary = True
        elif match := _HUNK.match(line):
            old, new = int(match[1]), int(match[2])
            hunk = Hunk(header=line)
            if not current.too_large:
                current.hunks.append(hunk)
        elif line.startswith("\\"):
            continue  # "\ No newline at end of file"
        else:
            sign, content = line[:1], line[1:]
            if sign == "+":
                item = DiffLine("add", content, new=new)
                current.additions += 1
                new += 1
            elif sign == "-":
                item = DiffLine("del", content, old=old)
                current.deletions += 1
                old += 1
            else:
                item = DiffLine("context", content, old=old, new=new)
                old += 1
                new += 1
            shown += 1
            if shown > max_file_lines:
                current.too_large, current.hunks = True, []
            elif not current.too_large:
                hunk.lines.append(item)
    return files
