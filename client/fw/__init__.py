"""fw: client of the aoi-flywheel platform (dataset versions now; models and inference jobs later).

For training scripts:

    import fw
    v = fw.get_version('SES-TX@3', '/data/train/SES-TX@3')   # lays the version out, returns its record
    print(v['id'], v['path'])
    ts = fw.get_trainset('SES-TX-det@2', '/data/train/SES-TX-det@2')   # a training-set snapshot
    print(ts['id'], ts['path'], ts['classes'])
"""
__version__ = '0.1.0'

from .api import ApiError  # noqa: E402
from .ops import FwError, get_trainset, get_version  # noqa: E402

__all__ = ['ApiError', 'FwError', 'get_trainset', 'get_version', '__version__']
