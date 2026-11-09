"""Run a TensorRT engine over the sets of an aoi-deteval sets config and write an aoi-deteval predictions file.

fw worker runs this file with the runner's conda environment (`conda run -n <env> python trt_predict.py ...`):
it imports only what that environment has (OpenCV and the deploy code's inference module), nothing of fw.

    --framework rfdetr: py_rfdetr.RfdetrDetector of the deploy code (rfdetrinfer). It returns
    [x, y, w, h, score, class] per box in pixels of the original image, (x, y) the top left corner, one box per
    query with its best class.

The predictions file is aoi-deteval's (schema_version 1): {"sets": {set: {image file name: [[x1, y1, x2, y2,
score, class], ...]}}}, with every image of every set present (an image without boxes has []). Classes the
engine has beyond the model's class table (--nc) are left out and counted; --stats gets the counts
({"images", "boxes", "dropped"}). Exit status 3: the engine could not be loaded (fw worker builds it again once).
"""
import argparse
import json
import os
import sys
from pathlib import Path

IMAGE_EXTS = {'.bmp', '.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp'}  # as fw.contract.IMAGE_EXTS


def rfdetr(engine: str, conf: float):
    import py_rfdetr
    detector = py_rfdetr.RfdetrDetector(model_path=engine, device_id=0, conf_threshold=conf, start_class_id=0)

    def detect(image):
        return [[x, y, x + w, y + h, score, cls] for x, y, w, h, score, cls in detector.detect(image)]
    return detect, detector.release


FRAMEWORKS = {'rfdetr': rfdetr}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--framework', required=True, choices=sorted(FRAMEWORKS))
    ap.add_argument('--engine', required=True)
    ap.add_argument('--sets', required=True, help='aoi-deteval sets config (JSON)')
    ap.add_argument('--name', required=True)
    ap.add_argument('--nc', type=int, required=True, help="number of classes in the model's class table")
    ap.add_argument('--conf', type=float, default=0.001)
    ap.add_argument('--out', required=True)
    ap.add_argument('--stats', help='where to write the counts (JSON)')
    args = ap.parse_args(argv)

    import cv2
    cfg = json.loads(Path(args.sets).read_text(encoding='utf-8'))
    try:
        detect, release = FRAMEWORKS[args.framework](args.engine, args.conf)
    except Exception as e:  # noqa: BLE001  (the deploy code raises RuntimeError and the like)
        print(f'加载不了引擎 {args.engine}：{type(e).__name__}: {e}', file=sys.stderr)
        return 3
    out_sets, images, boxes, dropped = {}, 0, 0, 0
    try:
        for set_name, folder in cfg['sets'].items():
            rows = {}
            for path in sorted(Path(folder).iterdir()):
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTS:
                    continue
                image = cv2.imread(str(path), cv2.IMREAD_COLOR)  # BGR, 8 bit, 3 channels
                if image is None:
                    print(f'读不了图片 {path}', file=sys.stderr)
                    return 2
                kept = []
                for x1, y1, x2, y2, score, cls in detect(image):
                    cls = int(cls)
                    if not 0 <= cls < args.nc:
                        dropped += 1
                        continue
                    kept.append([float(x1), float(y1), float(x2), float(y2), float(score), cls])
                rows[path.name] = kept
                images += 1
                boxes += len(kept)
            out_sets[set_name] = rows
    finally:
        release()
    pred = {'schema_version': 1, 'name': args.name, 'framework': f'{args.framework}-tensorrt',
            'weights': os.path.basename(args.engine), 'conf': args.conf, 'nc': args.nc, 'sets': out_sets}
    out = Path(args.out)
    tmp = out.with_name(out.name + '.tmp')
    tmp.write_text(json.dumps(pred, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, out)
    if args.stats:
        Path(args.stats).write_text(json.dumps({'images': images, 'boxes': boxes, 'dropped': dropped}),
                                    encoding='utf-8')
    print(f'推理了 {images} 张图，{boxes} 个框' + (f'；去掉 {dropped} 个类别超出模型类别表的框' if dropped else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
