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


# Hand-made ONNX protobuf (fw.onnx_check tests): fields as onnx.proto numbers them.
def varint(v):
    out = b''
    while True:
        b = v & 0x7f
        v >>= 7
        if v:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def ld(field, payload):
    return varint(field << 3 | 2) + varint(len(payload)) + payload


def vi(field, value):
    return varint(field << 3) + varint(value)


def tensor(name, *extra):
    return ld(8, name.encode()) + vi(2, 1) + ld(9, b'\x00' * 16) + b''.join(extra)  # name, data_type, raw_data


EXTERNAL = vi(14, 1)
LOCATION = ld(13, ld(1, b'location') + ld(2, b'../../home/fw/.config/fw/token'))


def onnx_model(graph):
    return vi(1, 8) + ld(2, b'pytorch') + ld(7, graph)
