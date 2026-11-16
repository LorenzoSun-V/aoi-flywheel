"""The task registry (fw.contract.TASKS) and what it must never change: a detection snapshot made from the same
data is the same, file for file and byte for byte, before and after tasks are added (docs/tasks.md)."""
import hashlib
import json

import helpers
from test_trainsets import H, fetch, label, make, png, pool

BOM = b'\xef\xbb\xbf'

# A snapshot built from fixed data: every way a detection label is read and written. Its manifest was taken on
# main df4572d, before the task registry; a change of either hash is a change of what detection snapshots hold.
GOLDEN_MANIFEST = '07dc7f4707aabba70a5b3ef1f60eef744889e56cae33e7364d704556ce0e8925'
GOLDEN_FILES = {
    'classes.txt': '6200939c2c7a4bff50ece13e7344df63c613059ec45dd9054ddb87cb5d3e0946',
    'coco/test.json': '98362aeeab8a49c77ebdf744d98ef781544e40e0b466622fb5e1b0d4e24cdcdb',
    'coco/train.json': '20f0f92a2739ead2726b5a159d8ff347862479e82c14116acdd41f1f8bb3ec1a',
    'coco/val.json': '52d2737d33deb0c914b9d15a1b8bdefda3c9b855119a48d1b70232a2381b1c78',
    'images/test/GOLD/L1/B/b1.png': '1b1a6fd06371f6d7274200ebfb94325463592242d965d4f5af0e26ab25c7ff66',
    'images/test/GOLD/L1/B/b2.png': 'cdc174ad8db7bda323f825cc7930638dabc403976a74f2b848ad54da74e84a64',
    'images/train/GOLD/L1/A/a2.png': '06fb58b5649842787876381bfd05d8bd3b59d44832d2610869abfcc64c59a115',
    'images/train/GOLD/L1/A/a3.png': 'd6bf60c9e3a8d8da41e5b106a260c54425d6fe61975eabb9709aa5a51b63dbf0',
    'images/train/GOLD/L1/A/m0.png': '8cff0bc235f2ddf1652ec0162c315862220fd93d2b99782736947b98cb7416ec',
    'images/train/GOLD/L1/A/m1.png': 'd94f36b1994fe7fe4cb4d8932a20ef8ac18b8e17067d420366bb4c41b0ff06ee',
    'images/train/GOLD/L1/A/m3.png': '54e8064f6eaafa0d1f0efe8c120d998fa244b895b3a546b9e858f66cd451357b',
    'images/train/GOLD/L1/A/m4.png': 'b225ef9b8bdabaf4140c4a2a2eb4bc93642124ef3ed5d5d55d10d0ee36a0d1fd',
    'images/train/GOLD/L1/A/m5.png': '26fcacd196e2baa24b521dd3b9d1f63d92069362692998b7678834b130432146',
    'images/train/GOLD/L1/A/n1.png': '8d9c9a88f162a27e8ba0411d529e93c41b48f04bcc95482b0d8f9d965df0931b',
    'images/train/GOLD/L1/A/t1.png': 'cc88bfbc40979bfe4ba42b758e845709691b50cf945cb92914d8844b2a569541',
    'images/train/GOLD/L1/A/t2.png': 'ce023d4a0bf939f92dc3bd22820f9024f9913b67776ea7d0cadf665476cdad9e',
    'images/val/GOLD/L1/A/a1.png': 'e08e63e702a2e845abd7927003ce8fc909f458ac0a6bf0dabc745a2f784f50be',
    'images/val/GOLD/L1/A/m2.png': '9bd92eec8cd1433f37a2301235727d78a7bccfb67c84731a2e27d553be547425',
    'images/val/GOLD/L1/A/n2.png': '2a03f89fb0c5dc8b3bab264f1764118947727d779a1e67356cf007f05bc7fd0c',
    'json/test/GOLD/L1/B/b1.json': '28b157337f157d62041de3f095d9825d6fcb08c265230626f2f488f068bc9b02',
    'json/test/GOLD/L1/B/b2.json': '71b20ba71d4464dfa2c0cc9074ed00319e51c8e2fc6005c94df6057d867ba6d2',
    'json/train/GOLD/L1/A/a2.json': '6a82bad4caca42f32d6f4f00fe4016265157894a5bff5c98c1b538505577fdf7',
    'json/train/GOLD/L1/A/a3.json': 'd5e1a73a82043f38355b2ab55b1458bf2750dcf84f4094e5dd5e43ba3307c6fb',
    'json/train/GOLD/L1/A/m0.json': '2f3427b459400cd0314ae4e7eaffde9fab0641a535dda68819435d9e5fcecefe',
    'json/train/GOLD/L1/A/m1.json': '2f3427b459400cd0314ae4e7eaffde9fab0641a535dda68819435d9e5fcecefe',
    'json/train/GOLD/L1/A/m3.json': '2f3427b459400cd0314ae4e7eaffde9fab0641a535dda68819435d9e5fcecefe',
    'json/train/GOLD/L1/A/m4.json': '2f3427b459400cd0314ae4e7eaffde9fab0641a535dda68819435d9e5fcecefe',
    'json/train/GOLD/L1/A/m5.json': '2f3427b459400cd0314ae4e7eaffde9fab0641a535dda68819435d9e5fcecefe',
    'json/train/GOLD/L1/A/t1.json': '89f3af2ce82dfc9848c3ca22ae8285ddf148cce38eaa612011f5a14a558153b3',
    'json/train/GOLD/L1/A/t2.json': 'c75fe7dc6bba641c6a1589a06e374c3df2b71f9d0552e2e430b154d9f1e34857',
    'json/val/GOLD/L1/A/a1.json': '2ce54bd918afd5155fbf1a420c5593c0cacf2d464360676241ed219da9455d6b',
    'json/val/GOLD/L1/A/m2.json': '2f3427b459400cd0314ae4e7eaffde9fab0641a535dda68819435d9e5fcecefe',
    'labels/test/GOLD/L1/B/b1.txt': 'e5472253634a5843b8a72c56a644e4014341e7d2a802683d2c476a2f9a069f48',
    'labels/test/GOLD/L1/B/b2.txt': 'a248b4514f29ff5a99c5aa50b773fd368cb6a64ac8ed0700578ef77037900a57',
    'labels/train/GOLD/L1/A/a2.txt': '644632ab9afb24132977bbe84b4b59370fc376359f109d9b4546e8b93577c85a',
    'labels/train/GOLD/L1/A/a3.txt': '723abc371fdf3742de8057e6dd60da7eecaba5aac1937fd6bb97626123915b73',
    'labels/train/GOLD/L1/A/m0.txt': '43b50c23f06e2d36c9fc0b35625b355869c873c118eda886f35e3ba8bab4e306',
    'labels/train/GOLD/L1/A/m1.txt': '43b50c23f06e2d36c9fc0b35625b355869c873c118eda886f35e3ba8bab4e306',
    'labels/train/GOLD/L1/A/m3.txt': '43b50c23f06e2d36c9fc0b35625b355869c873c118eda886f35e3ba8bab4e306',
    'labels/train/GOLD/L1/A/m4.txt': '43b50c23f06e2d36c9fc0b35625b355869c873c118eda886f35e3ba8bab4e306',
    'labels/train/GOLD/L1/A/m5.txt': '43b50c23f06e2d36c9fc0b35625b355869c873c118eda886f35e3ba8bab4e306',
    'labels/train/GOLD/L1/A/t1.txt': '013af000e48a13a4c013bea87f9f463b1ed4110dd0d7e3658c137d9d4268f4e5',
    'labels/train/GOLD/L1/A/t2.txt': 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
    'labels/val/GOLD/L1/A/a1.txt': '4c6ad2762970c2a944a68fcf9236d769c9c59a34c9260a017ec10cc705eefb13',
    'labels/val/GOLD/L1/A/m2.txt': '43b50c23f06e2d36c9fc0b35625b355869c873c118eda886f35e3ba8bab4e306',
    'trainset.json': '2034132fa8f20f46eb8fab8ce6d2b5ae3ad505f25104fc8e70e2c7215d518b73',
}


def golden_files() -> dict:
    rect = lambda name, x1, y1, x2, y2: {'label': name, 'points': [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],  # noqa: E731
                                         'shape_type': 'rectangle'}
    files = {
        # rectangles, one reaching outside the image (clipped)
        'A/a1.png': png('golden-a1'),
        'A/a1.json': label(shapes=[rect('scratch', 10, 20, 50, 60), rect('dent', -5, 30, 40, H + 9)]),
        # a polygon and a rotated box: their bounding boxes; a shape without shape_type is a polygon
        'A/a2.png': png('golden-a2'),
        'A/a2.json': label(shapes=[
            {'label': 'scratch', 'points': [[12.5, 8], [30, 15.25], [22, 40]], 'shape_type': 'polygon'},
            {'label': 'ng', 'points': [[40, 40], [60, 45], [55, 65], [35, 60]], 'shape_type': 'rotation',
             'direction': 0.25},
            {'label': 'dent', 'points': [[1, 2], [3, 9], [7, 4]]}]),
        # without imageWidth / imageHeight: the size from the image header
        'A/a3.png': png('golden-a3', 320, 200),
        'A/a3.json': label('ok', size=None),
        # YOLO txt, one with a UTF-8 BOM, one with only blank lines (no box)
        'A/t1.png': png('golden-t1'),
        'A/t1.txt': BOM + b'0 0.5 0.5 0.25 0.125\n1 0.1 0.2 0.3 0.4\n',
        'A/t2.png': png('golden-t2'),
        'A/t2.txt': b'\n\n',
        # negatives (no label)
        'A/n1.png': png('golden-n1'),
        'A/n2.png': png('golden-n2'),
        # a folder for test: COCO for the test split too
        'B/b1.png': png('golden-b1'),
        'B/b1.json': label('dent', 'ng'),
        'B/b2.png': png('golden-b2'),
        'B/b2.txt': b'6 0.25 0.75 0.5 0.5\n',
    }
    for i in range(6):  # enough samples for a val split
        files[f'A/m{i}.png'] = png(f'golden-m{i}')
        files[f'A/m{i}.json'] = label('scratch', 'ok')
    return files


def test_a_detection_snapshot_is_the_same_byte_for_byte(api, tmp_path, cache):
    """Fixed data (rectangles, a clipped box, a polygon, a rotated box, a shape without shape_type, a label
    without the image size, YOLO txt with a BOM and with blank lines only, negatives, a test folder, a class map, txt
    numbered by the pool's class table) make the snapshot they always made: the same manifest, file for file."""
    name = pool(api, tmp_path, golden_files(), site='GOLD', line='L1', name='golden-det-pool')
    v = make(api, 'golden-det-snap', [f'{name}@1'], classes=['scratch', 'dent', 'ng', 'ok'], seed=7,
             test_dirs=['B'], class_map={'dent': 'scratch'}, label_classes=helpers.CLASSES)
    tree = fetch(api, cache, v['ref'], tmp_path / 'out')
    files = {p: hashlib.sha256(d).hexdigest() for p, d in sorted(tree.items())}
    info = json.loads(tree['trainset.json'])
    assert info['task'] == 'det' and info['format'] == 'fw-trainset 1'
    assert api.get('/trainsets/golden-det-snap/versions/1')['trainset']['task'] == 'det'
    # every version says its task: a snapshot's, none for a pool; the lists of datasets too
    assert api.version('golden-det-snap', 1)['task'] == 'det' and api.version(name, 1)['task'] is None
    listed = {d['name']: d['latest']['task'] for d in api.get('/datasets', site='GOLD')}
    assert listed['golden-det-snap'] == 'det' and listed[name] is None
    assert (v['manifest_sha256'], files) == (GOLDEN_MANIFEST, GOLDEN_FILES)


def test_detection_is_the_only_task_that_can_be_chosen_so_far():
    """Every task is written down, only detection is enabled, and an enabled task has its label functions."""
    from fw.contract import TASKS, enabled_tasks, metrics_text, task_title
    from fwserver import formats
    assert list(TASKS) == ['det', 'obb', 'seg', 'semseg'] and enabled_tasks() == ['det']
    assert [task_title(t) for t in TASKS] == ['检测', '旋转框', '实例分割', '语义分割'] and task_title('x') == 'x'
    det = TASKS['det']
    assert (det.stored, det.pred_schema, det.key_metrics, det.main_metric, det.eval_tool) == (
        ('yolo', 'coco', 'json'), 1, ('mAP50', 'mAP50-95', 'P', 'R'), 'mAP50-95', 'aoi-deteval')
    assert formats.BOX_SHAPES == {'rectangle', 'polygon', 'rotation'}
    for t in enabled_tasks():
        assert formats.of(t).task == t
    assert metrics_text({'mAP50': 0.9, 'mAP50-95': 0.6, 'P': 1}) == 'mAP50 0.9，mAP50-95 0.6'


def test_a_task_not_yet_enabled_is_refused(api, make_api, tmp_path):
    """A model or a snapshot of a task that is only written down is refused with the tasks that can be chosen;
    an account that may not create models is refused first."""
    import pytest
    from fw.api import ApiError
    from fwserver import trainsets
    from fwserver.datasets import DatasetError
    helpers.register_place('GOLD', 'L1')
    with pytest.raises(ApiError, match='任务类型只能是 det（检测）') as e:
        api.create_model('M-obb-not-yet', 'GOLD', 'L1', 'obb', '')
    assert e.value.status == 400
    with pytest.raises(ApiError) as e:
        make_api('field').create_model('M-obb-field', 'GOLD', 'L1', 'det', '')
    assert e.value.status == 403
    with pytest.raises(DatasetError, match='任务类型只能是 det') as e:
        trainsets.check_options(trainsets.Options(['x@1'], ['a'], task='seg'))
    assert e.value.status == 400


def test_the_web_pages_name_the_tasks_as_the_registry_does():
    """app.js keeps a copy of the task fields its pages use; the two copies are the same."""
    import shutil
    import subprocess
    from pathlib import Path
    import pytest
    from fw.contract import TASKS
    if not shutil.which('node'):
        pytest.skip('node is not installed')
    app = (Path(__file__).resolve().parents[2] / 'server' / 'fwserver' / 'web' / 'app.js').as_uri()
    r = subprocess.run(['node', '--input-type=module', '-e', f"import {{ TASKS }} from '{app}'; console.log(JSON.stringify(TASKS))"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    web = json.loads(r.stdout)
    assert web == {t.id: {'title': t.title, 'enabled': t.enabled, 'key_metrics': list(t.key_metrics),
                          'main_metric': t.main_metric} for t in TASKS.values()}
