from setuptools import setup
import python_project_tools

setup(
    name=python_project_tools.__name__,
    url=python_project_tools.__url__,
    version=python_project_tools.__version__,
    author=python_project_tools.__author__,
    author_email=python_project_tools.__author_email__,
    description=python_project_tools.__description__,
    long_description=open('README.rst').read(),
    license=python_project_tools.__license__,
    packages=['python_project_tools'],
    test_suite='nose.collector',
    setup_requires=['nose'],
    install_requires=open('requirements.txt').readlines(),
    classifiers=[
        'License :: OSI Approved :: GNU General Public License (GPL)',
    ],
)

