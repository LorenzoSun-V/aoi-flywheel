"""fw: client of the aoi-flywheel platform (dataset versions, training-set snapshots, models; inference jobs
later).

For training scripts:

    import fw
    v = fw.get_version('SES-TX@3', '/data/train/SES-TX@3')   # lays the version out, returns its record
    print(v['id'], v['path'])
    ts = fw.get_trainset('SES-TX-det@2', '/data/train/SES-TX-det@2')   # a training-set snapshot
    print(ts['id'], ts['path'], ts['classes'])
    mv = fw.register_model('SES-TX-detr', onnx='best.onnx', trainset=ts, metrics='val.json')
    print(mv['ref'])   # SES-TX-detr@3

For deploy scripts:

    m = fw.get_model('SES-TX-detr@3', '/opt/deploy/models/SES-TX-detr@3')   # model.onnx, classes.txt, ...
"""
__version__ = '0.1.0'

from .api import ApiError  # noqa: E402
from .ops import FwError, get_trainset, get_version  # noqa: E402
from .ops import script_get_model as get_model  # noqa: E402
from .ops import script_register_model as register_model  # noqa: E402

__all__ = ['ApiError', 'FwError', 'get_model', 'get_trainset', 'get_version', 'register_model', '__version__']
