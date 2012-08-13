__version__ = '0.1'
__license__='BSD',
__author__ = 'Rick van Hattem'
__author_email__ = 'Rick.van.Hattem@Fawo.nl'
__organization_name__ = 'Karidu'
__organization_domain__ = 'karidu.org'
__application__ = 'Python Project Tools'
__name__ = __application__.lower().replace(' ', '-')
__author__ = 'Rick van Hattem'
__author_email__ = 'Rick.van.Hattem@Fawo.nl'
__description__ = ('Python Project Tools is a package that makes developing '
    'and deploying proper Python packages easier')
__url__ = 'https://github.com/WoLpH/python-project-tools/'
__long_description__ = '''
Python Project Tools is a package that makes developing and deploying proper
Python packages easier.

This package can be a starting point for new applications with templates for:
- Sphinx documentation
- Automated doctesting through nose
- Travis configuration for automated testing of the nose tests
- ReadTheDocs compatible configuration
- Uniform variables like author, website and more for your `setup.py` and
  Sphinx configuration.
- A complete `setup.py` including `MANIFEST.in` and documentated template
  explaining how to use `setup_requires`, `packages`, `entry_points` and more.
- Git repository with automatic Github project creation
- Creating and activating a virtualenv using virtualenvwrapper

It also has short commands for deploying your code:
- automatically running the test suite before deploying
- deploying your code to the Python Package Index (PyPI)
- deploying your code to git including creating a new tag for the release
- deploying the documentation to PyPI
- deploying the documentation to ReadTheDocs

While deploying it is possible to automatically sign the releases (both Git
and PyPI) with your own GPG key.
'''

