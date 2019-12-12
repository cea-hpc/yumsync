from distutils.util import convert_path
from setuptools import setup

main_metadata = {}
metadata_path = convert_path('yumsync/metadata.py')
with open(metadata_path) as metadata_file:
    exec(metadata_file.read(), main_metadata)

setup(
    name='yumsync',
    version=main_metadata['__version__'],
    description='A tool for mirroring and versioning YUM repositories',
    author='Ryan Uber, Vamegh Hedayati, Jordan Wesolowski',
    author_email='ru@ryanuber.com, repo@ev9.io, jrwesolo@gmail.com',
    url='https://github.com/jrwesolo/yumsync',
    packages=['yumsync'],
    scripts=['bin/yumsync'],
    install_requires=['blessings', 'PyYAML', 'six'],
    zip_safe=False
)
