#!/usr/bin/env python3

import argparse
import errno
import logging
import os
import stat
import sys

import common
from pyctr import util

try:
    from mount_ncch import NCCHContainerMount
except ImportError:
    print("Failed to import import_ncch, NCCH mount will not be available.")
    NCCHContainerMount = None

try:
    from fuse import FUSE, FuseOSError, Operations, LoggingMixIn, fuse_get_context
except ImportError:
    sys.exit("fuse module not found, please install fusepy to mount images "
             "(`pip3 install git+https://github.com/billziss-gh/fusepy.git`).")


class CTRCartImageMount(LoggingMixIn, Operations):
    fd = 0

    def __init__(self, cci_fp, dev, g_stat, seeddb=None):
        # get status change, modify, and file access times
        self.g_stat = {'st_ctime': int(g_stat.st_ctime),
                       'st_mtime': int(g_stat.st_mtime),
                       'st_atime': int(g_stat.st_atime)}

        # open cci and get section sizes
        self.f = cci_fp
        self.f.seek(0x100)
        ncsd_header = self.f.read(0x100)
        if ncsd_header[0:4] != b'NCSD':
            sys.exit('NCSD magic not found, is this a real CCI?')
        self.media_id = ncsd_header[0x8:0x10]
        if self.media_id == b'\0' * 8:
            sys.exit('Media ID is all-zero, is this a CCI?')

        self.cci_size = util.readle(ncsd_header[4:8]) * 0x200

        # create initial virtual files
        self.files = {'/ncsd.bin': {'size': 0x200, 'offset': 0},
                      '/cardinfo.bin': {'size': 0x1000, 'offset': 0x200},
                      '/devinfo.bin': {'size': 0x300, 'offset': 0x1200}}

        ncsd_part_raw = ncsd_header[0x20:0x60]
        ncsd_partitions = [[util.readle(ncsd_part_raw[i:i + 4]) * 0x200,
                            util.readle(ncsd_part_raw[i + 4:i + 8]) * 0x200] for i in range(0, 0x40, 0x8)]

        ncsd_part_names = ['game', 'manual', 'dlp', 'unk', 'unk', 'unk', 'update_n3ds', 'update_o3ds']

        self.dirs = {}
        for idx, part in enumerate(ncsd_partitions):
            if part[0]:
                filename = '/content{}.{}.ncch'.format(idx, ncsd_part_names[idx])
                self.files[filename] = {'size': part[1], 'offset': part[0]}

                dirname = '/content{}.{}'.format(idx, ncsd_part_names[idx])
                try:
                    content_vfp = common.VirtualFileWrapper(self, filename, part[1])
                    content_fuse = NCCHContainerMount(content_vfp, dev, g_stat=g_stat, seeddb=seeddb)
                    self.dirs[dirname] = content_fuse
                except Exception as e:
                    print("Failed to mount {}: {}: {}".format(filename, type(e).__name__, e))

    def flush(self, path, fh):
        return self.f.flush()

    def getattr(self, path, fh=None):
        first_dir = common.get_first_dir(path)
        if first_dir in self.dirs:
            return self.dirs[first_dir].getattr(common.remove_first_dir(path), fh)
        uid, gid, pid = fuse_get_context()
        if path == '/':
            st = {'st_mode': (stat.S_IFDIR | 0o555), 'st_nlink': 2}
        elif path.lower() in self.files:
            st = {'st_mode': (stat.S_IFREG | 0o444), 'st_size': self.files[path.lower()]['size'], 'st_nlink': 1}
        else:
            raise FuseOSError(errno.ENOENT)
        return {**st, **self.g_stat, 'st_uid': uid, 'st_gid': gid}

    def open(self, path, flags):
        self.fd += 1
        return self.fd

    def readdir(self, path, fh):
        first_dir = common.get_first_dir(path)
        if first_dir in self.dirs:
            return self.dirs[first_dir].readdir(common.remove_first_dir(path), fh)
        return ['.', '..'] + [x[1:] for x in self.files] + [x[1:] for x in self.dirs]

    def read(self, path, size, offset, fh):
        first_dir = common.get_first_dir(path)
        if first_dir in self.dirs:
            return self.dirs[first_dir].read(common.remove_first_dir(path), size, offset, fh)
        fi = self.files[path.lower()]
        real_offset = fi['offset'] + offset
        self.f.seek(real_offset)
        return self.f.read(size)

    def statfs(self, path):
        first_dir = common.get_first_dir(path)
        if first_dir in self.dirs:
            return self.dirs[first_dir].statfs(common.remove_first_dir(path))
        return {'f_bsize': 4096, 'f_blocks': self.cci_size // 4096, 'f_bavail': 0, 'f_bfree': 0,
                'f_files': len(self.files)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Mount Nintendo 3DS CTR Cart Image files.')
    parser.add_argument('--dev', help="use dev keys", action='store_const', const=1, default=0)
    parser.add_argument('--seeddb', help="path to seeddb.bin")
    parser.add_argument('--fg', '-f', help="run in foreground", action='store_true')
    parser.add_argument('--do', help="debug output (python logging module)", action='store_true')
    parser.add_argument('-o', metavar='OPTIONS', help="mount options")
    parser.add_argument('cci', help="CCI file")
    parser.add_argument('mount_point', help="mount point")

    a = parser.parse_args()
    opts = dict(common.parse_fuse_opts(a.o))

    if a.do:
        logging.basicConfig(level=logging.DEBUG)

    cci_stat = os.stat(a.cci)

    with open(a.cci, 'rb') as f:
        mount = CTRCartImageMount(cci_fp=f, dev=a.dev, g_stat=cci_stat, seeddb=a.seeddb)
        if common.macos or common.windows:
            opts['fstypename'] = 'CCI'
            if common.macos:
                opts['volname'] = "CTR Cart Image ({})".format(mount.media_id[::-1].hex().upper())
            elif common.windows:
                # volume label can only be up to 32 chars
                opts['volname'] = "CCI ({})".format(mount.media_id[::-1].hex().upper())
        fuse = FUSE(mount, a.mount_point, foreground=a.fg or a.do, ro=True, nothreads=True,
                    fsname=os.path.realpath(a.cci), **opts)
