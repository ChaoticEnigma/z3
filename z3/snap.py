import argparse
import functools
import logging
import operator
import subprocess

import boto

from z3.config import get_config


def cached(func):
    @functools.wraps(func)
    def wrapper(self, *a, **kwa):
        # ARGUMENTS AREN'T PART OF CACHE KEY
        # that's safe in this case, but might cause surprising bugs if you reuse it as is
        cache_key = func.__name__ + '_cached_value'
        if not hasattr(self, cache_key):
            val = func(self, *a, **kwa)
            setattr(self, cache_key, val)
        return getattr(self, cache_key)
    return wrapper


class Snapshot(object):
    CYCLE = 'cycle detected'
    MISSING_PARENT = 'missing parent'
    PARENT_BROKEN = 'parent broken'

    MISSING_LOCALLY = 'missing locally'
    OK_LOCALLY = 'OK'

    def __init__(self, key, manager, local):
        self.name = key.key
        self.metadata = key.metadata
        self.local = local
        self._mgr = manager
        self._reason_broken = None

    def __repr__(self):
        if self.is_full:
            return "<Snapshot {}/full>".format(self.name)
        else:
            parent = "/" + self.metadata.get("parent", "")
            return "<Snapshot {}{}>".format(self.name, parent)

    @property
    def is_full(self):
        return self.metadata.get('is_full') == 'true'

    @property
    def parent(self):
        parent_name = self.metadata.get('parent')
        return self._mgr.get(parent_name)

    @cached
    def _is_healthy(self, visited=frozenset()):
        if self.is_full:
            return True
        if self in visited:
            self._reason_broken = self.CYCLE
            return False  # we ended up with a cycle, abort
        if self.parent is None:
            self._reason_broken = self.MISSING_PARENT
            return False  # missing parent
        if not self.parent._is_healthy(visited.union([self])):
            if self.parent.reason_broken == self.CYCLE:
                self._reason_broken = self.CYCLE
            else:
                self._reason_broken = self.PARENT_BROKEN
            return False
        return True

    @property
    def is_healthy(self):
        return self._is_healthy()

    @property
    def reason_broken(self):
        if self.is_healthy:
            return
        return self._reason_broken

    @property
    def local_state(self):
        if self.local is None:
            return self.MISSING_LOCALLY
        return self.OK_LOCALLY


class LocalZFS(object):
    def _list_snapshots(self):
        return subprocess.check_output(
            ['zfs', 'list', '-Ht', 'snap', '-o',
             'name,used,refer,mountpoint,written'])

    def list_snapshots(self):
        try:
            snap = self._list_snapshots()
        except OSError as err:
            logging.error("unable to list local snapshots!")
            return {}
        vols = {}
        for line in snap.splitlines():
            name, used, refer, mountpoint, written = line.split('\t')
            vol_name, snap_name = name.split('@', 1)
            snapshots = vols.setdefault(vol_name, {})
            snapshots[snap_name] = {
                'name': name,
                'used': used,
                'refer': refer,
                'mountpoint': mountpoint,
                'written': written,
            }
        return vols


class SnapshotManager(object):
    def __init__(self, bucket, local, prefix=""):
        self.bucket = bucket
        self._local_snapshots = local.list_snapshots()
        self._snapshots = self._get_snapshots(prefix=prefix)

    def _get_snapshots(self, prefix):
        snapshots = {}
        for key in self.bucket.list(prefix):
            key = self.bucket.get_key(key.key)
            try:
                fs_name, snap_name = key.key.split("@", 1)
            except ValueError:
                logging.warning("skipping %r", key)
                continue
            local = self._local_snapshots.get(fs_name, {}).get(snap_name)
            snapshots[key.name] = Snapshot(key, manager=self, local=local)
        return snapshots

    def list(self):
        return sorted(self._snapshots.values(), key=operator.attrgetter('name'))

    def get(self, name):
        return self._snapshots.get(name)


def list_snapshots(bucket, prefix):
    mgr = SnapshotManager(bucket, LocalZFS(), prefix=prefix)
    fmt = "{:40} | {:15} | {:15} | {:10}"
    print fmt.format("NAME", "TYPE", "HEALTH", "LOCAL STATE")
    for snap in mgr.list():
        snap_type = 'full' if snap.is_full else 'incremental'
        health = snap.reason_broken or 'ok'
        print fmt.format(snap, snap_type, health, snap.local_state)


def main():
    cfg = get_config()
    parser = argparse.ArgumentParser(
        description='list z3 snapshots',
    )
    parser.add_argument('--prefix',
                        dest='prefix',
                        default='',
                        help='s3 key prefix')
    args = parser.parse_args()
    bucket = boto.connect_s3(
        cfg['S3_KEY_ID'], cfg['S3_SECRET']).get_bucket(cfg['BUCKET'])
    list_snapshots(bucket, prefix=args.prefix)


if __name__ == '__main__':
    main()