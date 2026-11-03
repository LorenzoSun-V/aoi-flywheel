import os
from pathlib import Path


def write(root: Path, rel: str, data: bytes) -> Path:
    path = Path(root).joinpath(*rel.split('/'))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def read_tree(root: Path) -> dict:
    """{relative posix path: bytes} of every file under root."""
    out = {}
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


def quiet(_):
    pass
