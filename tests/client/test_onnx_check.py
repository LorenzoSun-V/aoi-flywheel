"""fw.onnx_check: an ONNX that keeps tensor data in other files is refused (review of PR #13, round 4)."""
import pytest

from fw.onnx_check import OnnxError, check_self_contained, external_tensors
from helpers import EXTERNAL, LOCATION, ld, onnx_model as model, tensor, vi


def test_a_self_contained_model_passes(tmp_path):
    graph = ld(1, ld(4, b'Conv')) + ld(5, tensor('w')) + ld(5, tensor('b'))
    p = tmp_path / 'm.onnx'
    p.write_bytes(model(graph))
    check_self_contained(p)
    assert external_tensors(model(graph)) == []


@pytest.mark.parametrize('graph, names', [
    (ld(5, tensor('w', EXTERNAL)), ['w']),  # an initializer marked EXTERNAL
    (ld(5, tensor('w')) + ld(5, tensor('b', LOCATION)), ['b']),  # external_data without data_location
    (ld(15, ld(1, tensor('sv', EXTERNAL)) + ld(2, tensor('si'))), ['sv']),  # a sparse initializer
    (ld(1, ld(5, ld(1, b'value') + ld(5, tensor('c', EXTERNAL)))), ['c']),  # a Constant node's tensor
    (ld(1, ld(5, ld(1, b'then_branch') + ld(6, ld(5, tensor('deep', LOCATION))))), ['deep']),  # an If branch
])
def test_external_data_is_found_wherever_a_tensor_can_be(tmp_path, graph, names):
    assert external_tensors(model(graph)) == names
    p = tmp_path / 'm.onnx'
    p.write_bytes(model(graph))
    with pytest.raises(OnnxError, match='external data'):
        check_self_contained(p)


def test_in_functions_and_training_info():
    function = ld(7, ld(5, ld(1, b'value') + ld(5, tensor('f', EXTERNAL))))
    assert external_tensors(vi(1, 8) + ld(25, function)) == ['f']
    assert external_tensors(vi(1, 8) + ld(20, ld(1, ld(5, tensor('t', EXTERNAL))))) == ['t']
    # review of PR #13 round 5: a local function's attribute default (FunctionProto.attribute_proto, 11)
    default = ld(11, ld(1, b'value') + ld(5, tensor('d', LOCATION)))
    assert external_tensors(vi(1, 8) + ld(25, ld(1, b'f') + default)) == ['d']


@pytest.mark.parametrize('data', [b'', b'\x08', b'\x08\x07\x3a\x10abc', b'\x08\x07\x0f'])
def test_what_is_not_readable_as_onnx_is_refused(tmp_path, data):
    p = tmp_path / 'm.onnx'
    p.write_bytes(data)
    with pytest.raises(OnnxError):
        check_self_contained(p)
