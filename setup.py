import os
import sys

from distutils.core import setup,Extension
try:
	import py2exe
except ImportError:
	pass
	

def get_version():
    root_dir = os.path.abspath(os.path.dirname(sys.argv[0]))
    git_dir = os.path.join(root_dir, '.git')
    head = open(os.path.join(git_dir, 'HEAD')).read().strip()
    prefix = 'ref: '
    if head.startswith(prefix):
        path = head[len(prefix):].split('/')
        return open(os.path.join(git_dir, *path)).read().strip()[:7]
    else:
        return head[:7]

# Debian build doesn't easily support multiple Python packages.
# So we cheat.
ext = {}
if "DEB_BUILD_ARCH" in os.environ:
	ltc_scrypt_module = Extension('ltc_scrypt',
					sources = ['litecoin_scrypt/scryptmodule.c', 'litecoin_scrypt/scrypt.c'],
					include_dirs=['litecoin_scrypt'])
	ext["ext_modules"] = [ltc_scrypt_module]
else:
	# Debian packaging installs these itself
    ext["data_files"]=[('', ['README', 'README-Litecoin'])],

open('p2pool/__init__.py', 'wb').write('__version__ = %r\r\n\r\nDEBUG = False\r\n' % get_version())

setup(name='p2pool',
    version='1.0',
    description='Peer-to-peer Bitcoin mining pool',
    author='Forrest Voight',
    author_email='forrest@forre.st',
    url='http://p2pool.forre.st/',
    
    console=['run_p2pool.py'],
    options=dict(py2exe=dict(
        bundle_files=1,
        dll_excludes=['w9xpopen.exe'],
        includes=['twisted.web.resource', 'ltc_scrypt'],
    )),
    zipfile=None,
	packages=["p2pool","p2pool.bitcoin","p2pool.util","nattraverso"],
	scripts=["run_p2pool.py"],
	**ext
)
