"""An in-memory stand-in for the platform's HTTP API, with the same rules as the server. Used by the client
tests, which also run on Windows and Python 3.8 where the real server does not run."""
import hashlib

from fw.api import ApiError
from fw.contract import diff_entries, parse_manifest


class FakeApi:
    server = 'http://fake'

    def __init__(self):
        self.objects = {}
        self.datasets = {}  # name -> list of versions
        self.by_id = {}
        self.uploads = 0
        self.downloads = 0
        self.corrupt_next_download = set()

    # objects
    def missing(self, objects):
        return [d for d, s in objects if d not in self.objects or len(self.objects[d]) != s]

    def put_file(self, digest, path):
        with open(path, 'rb') as f:
            data = f.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ApiError(400, '上传的内容和哈希对不上')
        self.objects[digest] = data
        self.uploads += 1
        return len(data)

    def put_bytes(self, data):
        digest = hashlib.sha256(data).hexdigest()
        self.objects[digest] = data
        return digest

    def read_object(self, digest):
        return self.objects[digest]

    def download(self, digest, out):
        data = self.objects[digest]
        self.downloads += 1
        if digest in self.corrupt_next_download:
            self.corrupt_next_download.discard(digest)
            data = b'X' + data[1:]
        if hashlib.sha256(data).hexdigest() != digest:
            raise ApiError(0, '下载的文件和它的哈希对不上')
        out.seek(0)
        out.truncate()
        out.write(data)
        return len(data)

    # datasets and versions
    def create_dataset(self, name, site='', line='', kind='pool', note=''):
        self.datasets[name] = []
        return self.dataset(name)

    def dataset(self, name):
        if name not in self.datasets:
            raise ApiError(404, f'没有数据集 {name}')
        versions = self.datasets[name]
        return {'name': name, 'latest': versions[-1] if versions else None, 'version_count': len(versions)}

    def versions(self, name):
        return list(self.datasets[name])

    def version(self, name, number='latest'):
        versions = self.datasets[name]
        if number == 'latest':
            if not versions:
                raise ApiError(404, '还没有版本')
            return versions[-1]
        for v in versions:
            if v['number'] == int(number):
                return v
        raise ApiError(404, f'没有版本 {name}@{number}')

    def create_version(self, name, manifest_sha256, parent_id, expect_latest_id, source, note):
        versions = self.datasets[name]
        latest = versions[-1] if versions else None
        if (latest['id'] if latest else None) != expect_latest_id:
            raise ApiError(409, '数据集在这期间有了变化')
        entries = parse_manifest(self.objects[manifest_sha256])
        for p, d, s in entries:
            if d not in self.objects or len(self.objects[d]) != s:
                raise ApiError(400, f'{p} 服务器上没有')
        parent = self.by_id.get(parent_id) if parent_id is not None else None
        if parent is not None and parent['manifest_sha256'] == manifest_sha256:
            raise ApiError(409, '内容和父版本完全一样')
        number = len(versions) + 1
        vid = len(self.by_id) + 1
        v = {'id': vid, 'dataset': name, 'number': number, 'ref': f'{name}@{number}',
             'parent': {k: parent[k] for k in ('id', 'dataset', 'number', 'ref')} if parent else None,
             'manifest_sha256': manifest_sha256, 'file_count': len(entries),
             'total_bytes': sum(s for _, _, s in entries), 'source': source, 'note': note,
             'created_by': 'tester', 'created_at': '2026-11-03T08:00:00+00:00'}
        versions.append(v)
        self.by_id[vid] = v
        return v

    def diff(self, old_id, new_id):
        old = parse_manifest(self.objects[self.by_id[old_id]['manifest_sha256']])
        new = parse_manifest(self.objects[self.by_id[new_id]['manifest_sha256']])
        return diff_entries(old, new)

    def close(self):
        pass
