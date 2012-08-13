import logging
import _registry

__all__ = [
    'CreatePlugin',
    'DeployPlugin',
    'TestPlugin',
]

logger = logging.getLogger(__name__)

if __name__ == '__main__':
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

class PluginsMeta(type):
    def __init__(cls, name, bases, dict_):
        type.__init__(cls, name, bases, dict)
        metaclass = dict_.get('__metaclass__')
        if not metaclass:
            for base in bases:
                metaclass = getattr(base, '__metaclass__')
                if metaclass:
                    break

        assert metaclass, ('All plugins must have a `PluginsMeta` inheriting '
            'metaclass')
        metaclass_name = metaclass.get_name()
        if metaclass_name:
            _registry.plugins[metaclass_name].append(cls)


    @classmethod
    def get_name(cls):
        return cls.__name__.replace('PluginsMeta', '', 1).lower()

    @classmethod
    def get_plugins(cls, type_=None):
        return _registry.plugins[cls.get_name()]


class CreatePluginsMeta(PluginsMeta):
    pass


class DeployPluginsMeta(PluginsMeta):
    pass


class TestPluginsMeta(PluginsMeta):
    pass


class Plugin(object):
    __metaclass__ = PluginsMeta

    def __init__(self, environment):
        self.environment = environment

    def __repr__(self):
        return (u'<%s: %r>' % (
            self.__class__.__name__,
            self.environment,
        )).encode('utf-8', 'replace')

class CreatePlugin(Plugin):
    __metaclass__ = CreatePluginsMeta


class DeployPlugin(Plugin):
    __metaclass__ = DeployPluginsMeta


class TestPlugin(Plugin):
    __metaclass__ = TestPluginsMeta

