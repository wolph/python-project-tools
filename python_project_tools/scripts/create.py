import logging

if __name__ == '__main__':
    handler = logging.StreamHandler()
    handler.setLevel(logging.DEBUG)
    logger = logging.getLogger('')
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)


from ..plugins import plugins

print plugins

