import atexit
import cherrypy
import errno
import fuse
import os
import six
import stat
import subprocess
import threading
import time

from girder import events, logger, logprint
from girder.constants import AccessType
from girder.models.model_base import AccessException, ValidationException
from girder.utility import config
from girder.utility.model_importer import ModelImporter
from girder.utility import path as path_util


fuseMounts = {}


class ResourceFuse(fuse.Operations, ModelImporter):
    log = logger

    def __init__(self, name):
        super(ResourceFuse, self).__init__()
        self.name = name
        self.nextFH = 1
        self.openFiles = {}

    def __call__(self, op, path, *args):
        if self.log:
            self.log.debug('-> %s %s %s', op, path, repr(args))
        ret = '[exception]'
        try:
            ret = getattr(self, op)(path, *args)
            return ret
        except OSError as e:
            if self.log:
                if getattr(e, 'errno', None) == errno.ENOENT:
                    self.log.debug('-- %s %s', op, str(e))
                else:
                    self.log.exception('-- %s', op)
            raise
        except Exception as e:
            if self.log:
                if getattr(e, 'errno', None) == errno.ENOENT:
                    self.log.debug('-- %s %s', op, str(e))
                else:
                    self.log.exception('-- %s', op)
            raise
        finally:
            if self.log:
                if op != 'read':
                    self.log.debug('<- %s %s', op, repr(ret))
                else:
                    self.log.debug('<- %s (length %d) %s', op, len(ret), repr(ret[:16]))

    def _getPath(self, path):
        try:
            resource = path_util.lookUpPath(
                path.rstrip('/'), filter=False,
                user=fuseMounts[self.name]['user'],
                force=fuseMounts[self.name]['force'])
        except KeyError:  # This can be triggered when the mount is removed
            raise fuse.FuseOSError(errno.ENOENT)
        except path_util.NotFoundException:
            raise fuse.FuseOSError(errno.ENOENT)
        except ValidationException:
            raise fuse.FuseOSError(errno.EROFS)
        except AccessException:
            raise fuse.FuseOSError(errno.ENOENT)
        except Exception:
            self.log.exception('ResourceFuse server internal error')
            raise fuse.FuseOSError(errno.EROFS)
        return resource   # {model, document}

    def _stat(self, doc, model):
        attr = fuseMounts[self.name]['stat'].copy()
        attr['st_ino'] = -1
        attr['st_nlink'] = 1
        if 'updated' in doc:
            attr['st_mtime'] = time.mktime(doc['updated'].timetuple())
        elif 'created' in doc:
            attr['st_mtime'] = time.mktime(doc['created'].timetuple())
        attr['st_ctime'] = attr['st_mtime']

        if model == 'file':
            attr['st_mode'] = 0o777 | stat.S_IFREG
            attr['st_size'] = doc.get('size', len(doc.get('linkUrl', '')))
        else:
            attr['st_mode'] = 0o777 | stat.S_IFDIR
            attr['st_size'] = 0
        return attr

    def _name(self, doc, model):
        name = path_util.getResourceName(model, doc)
        if isinstance(name, six.binary_type):
            name = name.decode('utf8')
        return name

    def _list(self, doc, model):
        entries = []
        if model in ('collection', 'user', 'folder'):
            if fuseMounts[self.name]['force']:
                folderList = self.model('folder').find({
                    'parentId': doc['_id'],
                    'parentCollection': model.lower()
                })
            else:
                folderList = self.model('folder').childFolders(
                    parent=doc, parentType=model,
                    user=fuseMounts[self.name]['user'])
            for folder in folderList:
                entries.append(self._name(folder, 'folder'))
        if model == 'folder':
            for item in self.model('folder').childItems(doc):
                entries.append(self._name(item, 'item'))
        elif model == 'item':
            for file in self.model('item').childFiles(doc):
                entries.append(self._name(file, 'file'))
        return entries

    getxattr = None
    listxattr = None

    def access(self, path, mode):
        # mode is either F_OK or a bitfield of R_OK, W_OK, X_OK
        # we need to validate if the resource can be accessed
        resource = self._getPath(path)
        if mode != os.F_OK and not fuseMounts[self.name]['force']:
            if (mode & os.R_OK):
                self.model(resource['model']).requireAccess(
                    resource['document'], fuseMounts[self.name]['user'], level=AccessType.READ)
            if (mode & os.W_OK):
                self.model(resource['model']).requireAccess(
                    resource['document'], fuseMounts[self.name]['user'], level=AccessType.WRITE)
            if (mode & os.X_OK):
                self.model(resource['model']).requireAccess(
                    resource['document'], fuseMounts[self.name]['user'], level=AccessType.ADMIN)
        return True

    def getattr(self, path, fh=None):
        if path.rstrip('/') in ('', '/user', '/collection'):
            attr = fuseMounts[self.name]['stat'].copy()
            attr['st_mode'] = 0o777 | stat.S_IFDIR
            attr['st_size'] = 0
        else:
            resource = self._getPath(path)
            attr = self._stat(resource['document'], resource['model'])
        return attr

    def read(self, path, size, offset, fh):
        if fh not in self.openFiles:
            raise fuse.FuseOSError(errno.EBADF)
        with self.openFiles[fh]['lock']:
            handle = self.openFiles[fh]['handle']
            handle.seek(offset)
            return handle.read(size)

    def readdir(self, path, fh):
        path = path.rstrip('/')
        result = [u'.', u'..']
        if path == '':
            result.extend([u'collection', u'user'])
        elif path in ('/user', '/collection'):
            model = path[1:]
            if fuseMounts[self.name]['force']:
                docList = self.model(model).find({}, sort=None)
            else:
                docList = self.model(model).list(user=fuseMounts[self.name]['user'])

            for doc in docList:
                result.append(self._name(doc, model))
        else:
            resource = self._getPath(path)
            result.extend(self._list(resource['document'], resource['model']))
        return result

    def open(self, path, flags):
        resource = self._getPath(path)
        if resource['model'] != 'file':
            return super(ResourceFuse, self).open(path, flags)
        fh = self.nextFH
        self.nextFH += 1
        self.openFiles[fh] = {
            'path': path,
            'handle': ModelImporter.model('file').open(resource['document']),
            'lock': threading.Lock(),
        }
        return fh

    def release(self, path, fh):
        if fh in self.openFiles:
            if 'handle' in self.openFiles[fh]:
                self.openFiles[fh]['handle'].close()
            del self.openFiles[fh]
        return 0

    def destroy(self, path):
        events.trigger('resource_fuse.destroy', {'path': path})


@atexit.register
def unmountAll():
    events.trigger('resource_fuse.unmount', {'name': None})
    for name in fuseMounts.keys():
        unmountResourceFuse(name)


def unmountResourceFuse(name):
    entry = fuseMounts.get(name, None)
    if entry:
        events.trigger('resource_fuse.unmount', {'name': name})
        path = entry['path']
        subprocess.call(['fusermount', '-u', os.path.realpath(path)])
        if entry['thread']:
            entry['thread'].join(10)
        # clean up previous processes so there aren't any zombies
        os.waitpid(-1, os.WNOHANG)
        fuseMounts.pop(name, None)


def mountResourceFuse(name, path, level=AccessType.ADMIN, user=None, force=False):
    if name in fuseMounts:
        if (fuseMounts[name]['level'] == level and
                fuseMounts[name]['user'] == user and
                fuseMounts[name]['force'] == force):
            return
        unmountResourceFuse(name)
    fuseMounts[name] = {
        'level': level,
        'user': user,
        'force': force,
        'path': path,
        'stat': dict((key, getattr(os.stat(path), key)) for key in (
            'st_atime', 'st_ctime', 'st_gid', 'st_mode', 'st_mtime',
            'st_nlink', 'st_size', 'st_uid')),
        'thread': None
    }
    try:
        # We run the file system in a thread, but as a foreground process.
        # This allows multiple mounted fuses to play well together and stop
        # when the program is stopped.
        fuseThread = threading.Thread(
            target=fuse.FUSE, args=(ResourceFuse(name), path), kwargs={
                'foreground': True,
                # Automatically unmount when python we try to mount again
                'auto_unmount': True,
                # Cache files if their size and timestamp haven't changed
                'auto_cache': True,
                # We aren't specifying our own inos
                'use_ino': False,
                # read-only file system
                'ro': True,
            })
        fuseThread.daemon = True
        fuseThread.start()
        fuseMounts[name]['thread'] = fuseThread
        logprint.info('Mounted %s at %s' % (name, path))
    except Exception:
        logger.exception('Failed to mount %s at %s' % (name, path))
        fuseMounts.pop(name, None)


def getResourceFusePath(name, type, doc):
    """
    Given a fuse name and a resource, return the file path.

    :param name: name used for the fuse mount.
    :param type: the resource model type.
    :param doc: the resource document.
    :return: a path to the resource.
    """
    if name not in fuseMounts:
        return None
    return fuseMounts[name]['path'].rstrip('/') + path_util.getResourcePath(
        type, doc, user=fuseMounts[name]['user'], force=fuseMounts[name]['force'])


def startFromConfig():
    cfg = config.getConfig().get('resource_fuse', {})
    paths = cfg.get('paths', {})
    cherrypy.engine.subscribe('stop', unmountAll)

    if isinstance(paths, dict):
        for name in paths:
            if not os.path.isdir(paths[name]):
                logger.info('Can\'t mount resource fuse except to a directory: %s' % paths[name])
            mountResourceFuse(name, paths[name], force=True)


path_util.getResourceFusePath = getResourceFusePath
