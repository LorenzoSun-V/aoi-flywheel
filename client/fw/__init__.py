"""fw: client of the aoi-flywheel platform (dataset versions now; models and inference jobs later).

For training scripts:

    import fw
    v = fw.get_version('SES-TX@3', '/data/train/SES-TX@3')   # lays the version out, returns its record
    print(v['id'], v['path'])
"""
__version__ = '0.1.0'

from .api import ApiError  # noqa: E402
from .ops import FwError, get_version  # noqa: E402

__all__ = ['ApiError', 'FwError', 'get_version', '__version__']
