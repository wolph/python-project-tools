import logging

if __name__ == '__main__':
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger('')
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


from ..plugins import plugins

__all__ = ['plugins']

def main():
    pass

def get_subparsers(subparser):
    subparser.set_defaults(func=main)

