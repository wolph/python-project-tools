import os
import glob
import logging
import argparse

# default logging levels
# -qq means FATAL
# -q means ERROR
# No arguments means WARNING
# -v means INFO
# -vv means DEBUG
VERBOSITY = [
    logging.FATAL,
    logging.ERROR,
    logging.WARNING,
    logging.INFO,
    logging.DEBUG,
]

def setup_logger(verbosity, namespace=''):
    '''Create a logger for the given namespace and verbosity
    
    :param verbosity: The verbosity for this logger
        The range for verbosity is the indexes of the items in `VERBOSITY`
    :type verbosity: int
    :param namespace: The namespace for the logger, defaults to the global
        namespace
    :type verbosity: str
    '''
    # Make sure the verbosity is within accepted ranges
    verbosity = min(verbosity, len(VERBOSITY))
    verbosity = max(verbosity, 0)
    level = VERBOSITY[verbosity]

    # Create the logger for this namespace and set the level
    logger = logging.getLogger(namespace)
    logger.setLevel(level)

    # Create a handler with the given level and add it to the logger
    handler = logging.StreamHandler()
    handler.setLevel(level)
    logger.addHandler(handler)

    return logger

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--verbose', '-v', action='count')
    parser.add_argument('--quiet', '-q', action='count')

    subparsers = parser.add_subparsers(
        title='Scripts',
        help='The script to run',
    )
    get_subparsers(subparsers)

    args, _ = parser.parse_known_args()
    verbosity = 2
    verbosity += args.verbose or 0
    verbosity -= args.quiet or 0
    setup_logger(verbosity)

def get_subparsers(subparsers):
    pattern = os.path.join(os.path.dirname(__file__), 'scripts', '*.py')
    names = []
    for file_ in glob.glob(pattern):
        name = os.path.split(os.path.splitext(file_)[0])[1]
        if not name.startswith('__'):
            names.append(name)

    scripts_module = __import__('python_project_tools.scripts', locals(),
        globals(), names)
    
    for name in names:
        module = getattr(scripts_module, name)
        parser = subparsers.add_parser(name)
        getattr(module, 'get_subparsers')(parser)

if __name__ == '__main__':
    main()

