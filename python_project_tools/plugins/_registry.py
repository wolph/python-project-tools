import os
import glob
import pprint
import logging
import collections

logger = logging.getLogger(__name__)

class Plugins(collections.defaultdict):
    def __init__(self):
        super(Plugins, self).__init__(list)

    def load_plugins(self, *paths):
        if not paths:
            paths = (
                (
                    __name__.rsplit('.', 1)[0],
                    os.path.dirname(__file__),
                ),
            )

        for item in paths:
            if isinstance(item, basestring):
                from_, path  = item, None
            else:
                from_, path = item

            if not path:
                try:
                    from_parts = from_.rsplit('.', 1)
                    name = from_parts[-1]
                    if from_parts[1:]:
                        import_ = __import__(from_parts[0], fromlist=[name])
                    else:
                        import_ = __import__(name)
                    path = os.path.dirname(import_.__file__)
                except ImportError:
                    logger.exception('Unable to import %r', from_)
                else:
                    logger.info('Detected %r as path for %r', path, from_)
                    self.load_plugins_from_path(from_, path)
            elif os.path.isdir(path):
                self.load_plugins_from_path(from_, path)
            else:
                logger.warn('Plugin path %r does not exist', path)

    def load_plugins_from_path(self, from_, path):
        pattern = os.path.join(path, '*.py')
        logger.info('Loading plugins from %r', pattern)
        for file_ in glob.iglob(pattern):
            file_ = os.path.basename(file_)
            file_ = os.path.splitext(file_)[0]
            if file_.startswith('_'):
                logger.debug('Skipping %r', file_)
            else:
                logger.debug('Importing %r from %r', file_, from_)
                __import__(from_, fromlist=[file_])

    def __getattr__(self, attr):
        if attr in self:
            return self[attr]
        else:
            super(Plugins, self).__getattr__(attr)

    def __repr__(self):
        return (u'<%s: %s>' % (
            self.__class__.__name__,
            pprint.pformat(dict(self)),
        )).encode('utf-8', 'replace')

plugins = Plugins()
plugins.load_plugins()

