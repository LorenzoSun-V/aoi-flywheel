"""Refuse ONNX files that point outside themselves.

An ONNX tensor may keep its data in another file ("external data": TensorProto.data_location = EXTERNAL and
TensorProto.external_data with a location). TensorRT's ONNX parser then reads that file, by a path the model
chooses, while the engine is built on the GPU machine: a model could read files of the worker that way and carry
them into the engine and its results. The platform keeps a model as one file, model.onnx, so any external data
is refused, before registering and again before an engine is built.

This walks the protobuf by hand (no onnx package needed) through every place a TensorProto can be: the graph's
initializers and sparse initializers, node attributes (tensor, tensors, sparse tensors, subgraphs of If/Loop/Scan),
functions (their nodes and attribute defaults), training info. Tensor data (raw_data and the like) is skipped, not read.
"""
import mmap
from pathlib import Path
from typing import Iterator, List, Tuple

VARINT, I64, LEN, I32 = 0, 1, 2, 5
EXTERNAL = 1  # TensorProto.DataLocation.EXTERNAL


class OnnxError(ValueError):
    pass


def _varint(buf, pos: int, end: int) -> Tuple[int, int]:
    value = shift = 0
    while True:
        if pos >= end or shift > 63:
            raise OnnxError('ONNX 文件不完整或者不是 ONNX（protobuf 读不通）')
        b = buf[pos]
        pos += 1
        value |= (b & 0x7f) << shift
        if not b & 0x80:
            return value, pos
        shift += 7


def _fields(buf, start: int, end: int) -> Iterator[Tuple[int, int, int, int]]:
    """(field number, wire type, value or payload start, payload end) of one message."""
    pos = start
    while pos < end:
        key, pos = _varint(buf, pos, end)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise OnnxError('ONNX 文件不完整或者不是 ONNX（protobuf 读不通）')
        if wire == VARINT:
            value, pos = _varint(buf, pos, end)
            yield number, wire, value, pos
        elif wire == LEN:
            size, pos = _varint(buf, pos, end)
            if pos + size > end:
                raise OnnxError('ONNX 文件不完整或者不是 ONNX（protobuf 读不通）')
            yield number, wire, pos, pos + size
            pos += size
        elif wire in (I64, I32):
            pos += 8 if wire == I64 else 4
            if pos > end:
                raise OnnxError('ONNX 文件不完整或者不是 ONNX（protobuf 读不通）')
            yield number, wire, 0, pos
        else:
            raise OnnxError('ONNX 文件不完整或者不是 ONNX（protobuf 读不通）')


# Which fields of which message hold which message (onnx.proto, IR version 10). Repeated and single fields alike.
_HOLDS = {
    'model': {7: 'graph', 20: 'training', 25: 'function'},
    'training': {1: 'graph', 2: 'graph'},
    'function': {7: 'node', 11: 'attribute'},  # nodes, and attribute defaults (attribute_proto)
    'graph': {1: 'node', 5: 'tensor', 15: 'sparse'},
    'node': {5: 'attribute'},
    'attribute': {5: 'tensor', 6: 'graph', 10: 'tensor', 11: 'graph', 22: 'sparse', 23: 'sparse'},
    'sparse': {1: 'tensor', 2: 'tensor'},
}


def external_tensors(buf, start: int = 0, end: int = None) -> List[str]:
    """Names of the tensors (as far as they have one) that keep their data outside the file."""
    end = len(buf) if end is None else end
    found: List[str] = []
    todo = [('model', start, end, 0)]
    while todo:
        kind, s, e, depth = todo.pop()
        if depth > 64:
            raise OnnxError('ONNX 文件的子图嵌套太深')
        if kind == 'tensor':
            name, external = '', False
            for number, wire, a, b in _fields(buf, s, e):
                if number == 8 and wire == LEN:
                    name = bytes(buf[a:b]).decode('utf-8', 'replace')
                elif (number == 14 and wire == VARINT and a == EXTERNAL) or (number == 13 and wire == LEN):
                    external = True
            if external:
                found.append(name or '（没名字）')
            continue
        holds = _HOLDS[kind]
        for number, wire, a, b in _fields(buf, s, e):
            if wire == LEN and number in holds:
                todo.append((holds[number], a, b, depth + 1))
    return found


def check_self_contained(path: Path) -> None:
    """Raise OnnxError when the ONNX file is not readable as ONNX or keeps tensor data in other files."""
    with open(path, 'rb') as f:
        if f.seek(0, 2) == 0:
            raise OnnxError('ONNX 文件是空的')
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            names = external_tensors(m)
    if names:
        shown = '、'.join(names[:5]) + (f' 等 {len(names)} 个' if len(names) > 5 else '')
        raise OnnxError(f'ONNX 里有张量把数据放在别的文件里（external data：{shown}）；平台只收单个文件的 '
                        'ONNX，请导出时不用 external data（模型小于 2GB 时 torch.onnx.export 默认就是单文件）')
