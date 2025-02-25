#!/usr/bin/env python

import collections
import os
import pathlib
import re
import subprocess
import sys
import sysconfig
import warnings

# Required third-party imports, must be specified in pyproject.toml.
from setuptools import setup, Extension
import distutils.sysconfig
import numpy as np
from Cython.Build import cythonize
from Cython.Distutils import build_ext


def process_options():
    """
    Determine all runtime options, returning a dictionary of the results.  The
    keys are:
        'rootdir': str
            The root directory of the setup.  Almost certainly the directory
            that this setup.py file is contained in.
        'release': bool
            Is this a release build (True) or a local development build (False)
        'openmp': bool
            Should we build our OpenMP extensions and attempt to link in OpenMP
            libraries?
        'cflags': list of str
            Flags to be passed to the C++ compiler.
        'ldflags': list of str
            Flags to be passed to the linker.
        'include': list of str
            Additional directories to be added to the header files include
            path.  These files will be detected by Cython as dependencies, so
            changes to them will trigger recompilation of .pyx files, whereas
            includes added in 'cflags' as '-I/path/to/include' may not.
    """
    options = {}
    options['rootdir'] = os.path.dirname(os.path.abspath(__file__))
    options = _determine_user_arguments(options)
    options = _determine_version(options)
    options = _determine_compilation_options(options)
    return options


def _determine_user_arguments(options):
    """
    Add the 'release' and 'openmp' options to the collection, based on the
    passed command-line arguments or environment variables.
    """
    options['release'] = (
        '--release' in sys.argv
        or bool(os.environ.get('CI_QUTIP_RELEASE'))
    )
    if '--release' in sys.argv:
        sys.argv.remove('--release')
    options['openmp'] = (
        '--with-openmp' in sys.argv
        or bool(os.environ.get('CI_QUTIP_WITH_OPENMP'))
    )
    if "--with-openmp" in sys.argv:
        sys.argv.remove("--with-openmp")
    return options


def _determine_compilation_options(options):
    """
    Add additional options specific to C/C++ compilation.  These are 'cflags',
    'ldflags' and 'include'.
    """
    # Remove -Wstrict-prototypes from the CFLAGS variable that the Python build
    # process uses in addition to user-specified ones; the flag is not valid
    # for C++ compiles, but CFLAGS gets appended to those compiles anyway.
    config = distutils.sysconfig.get_config_vars()
    if "CFLAGS" in config:
        config["CFLAGS"] = config["CFLAGS"].replace("-Wstrict-prototypes", "")
    options['cflags'] = []
    options['ldflags'] = []
    options['include'] = [np.get_include()]
    if (
        sysconfig.get_platform().startswith("win")
        and os.environ.get('MSYSTEM') is None
    ):
        # Visual Studio
        options['cflags'].extend(['/w', '/Ox'])
        if options['openmp']:
            options['cflags'].append('/openmp')
    else:
        # Everything else
        options['cflags'].extend(['-w', '-O3', '-funroll-loops'])
    if sysconfig.get_platform().startswith("macos"):
        # These are needed for compiling on OSX 10.14+
        options['cflags'].append('-mmacosx-version-min=10.9')
        options['ldflags'].append('-mmacosx-version-min=10.9')
        if options['openmp']:
            options['cflags'].append('-fopenmp')
            options['ldflags'].append('-fopenmp')
    return options


def _determine_version(options):
    """
    Adds the 'short_version' and 'version' options.

    Read from the VERSION file to discover the version.  This should be a
    single line file containing valid Python package public identifier (see PEP
    440), for example
      4.5.2rc2
      5.0.0
      5.1.1a1
    We do that here rather than in setup.cfg so we can apply the local
    versioning number as well.
    """
    version_filename = os.path.join(options['rootdir'], 'VERSION')
    with open(version_filename, "r") as version_file:
        version = options['short_version'] = version_file.read().strip()
    VERSION_RE = r'\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?'
    if re.fullmatch(VERSION_RE, version, re.A) is None:
        raise ValueError("invalid version: " + version)
    if not options['release']:
        version += "+"
        try:
            git_out = subprocess.run(
                ('git', 'rev-parse', '--verify', '--short=7', 'HEAD'),
                check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            git_hash = git_out.stdout.decode(sys.stdout.encoding).strip()
            version += git_hash or "nogit"
        except subprocess.CalledProcessError:
            version += "nogit"
    options['version'] = version
    return options


def create_version_py_file(options):
    """
    Generate and write out the file qutip/version.py, which is used to produce
    the '__version__' information for the module.  This function will overwrite
    an existing file at that location.
    """
    filename = os.path.join(options['rootdir'], 'qutip', 'version.py')
    content = "\n".join([
        f"# This file is automatically generated by QuTiP's setup.py.",
        f"short_version = '{options['short_version']}'",
        f"version = '{options['version']}'",
        f"release = {options['release']}",
    ])
    with open(filename, 'w') as file:
        print(content, file=file)


def _extension_extra_sources():
    """
    Get a mapping of {module: extra_sources} for all modules to be built.  The
    module is the fully qualified Python module (e.g. 'qutip.cy.spmatfuncs'),
    and extra_sources is a list of strings of relative paths to files.  If no
    extra sources are known for a given module, the mapping will return an
    empty list.
    """
    # For typing brevity we specify sources in Unix-style string form, then
    # normalise them into the OS-specific form later.
    extra_sources = {
        'qutip.cy.spmatfuncs': ['qutip/cy/src/zspmv.cpp'],
        'qutip.cy.openmp.parfuncs': ['qutip/cy/openmp/src/zspmv_openmp.cpp'],
    }
    out = collections.defaultdict(list)
    for module, sources in extra_sources.items():
        # Normalise the sources into OS-specific form.
        out[module] = [str(pathlib.Path(source)) for source in sources]
    return out


def create_extension_modules(options):
    """
    Discover and Cythonise all extension modules that need to be built.  These
    are returned so they can be passed into the setup command.
    """
    out = []
    root = pathlib.Path(options['rootdir'])
    pyx_files = set(root.glob('qutip/**/*.pyx'))
    if not options['openmp']:
        pyx_files -= set(root.glob('qutip/**/openmp/**/*.pyx'))
    extra_sources = _extension_extra_sources()
    # Add Cython files from qutip
    for pyx_file in pyx_files:
        pyx_file = pyx_file.relative_to(root)
        pyx_file_str = str(pyx_file)
        if 'compiled_coeff' in pyx_file_str or 'qtcoeff_' in pyx_file_str:
            # In development (at least for QuTiP ~4.5 and ~5.0) sometimes the
            # Cythonised time-dependent coefficients would get dropped in the
            # qutip directory if you weren't careful - this is just trying to
            # minimise the occasional developer error.
            warnings.warn(
                "skipping generated time-dependent coefficient: "
                + pyx_file_str
            )
            continue
        # The module name is the same as the folder structure, but with dots in
        # place of separators ('/' or '\'), and without the '.pyx' extension.
        pyx_module = ".".join(pyx_file.parts)[:-4]
        pyx_sources = [pyx_file_str] + extra_sources[pyx_module]
        out.append(Extension(pyx_module,
                             sources=pyx_sources,
                             include_dirs=options['include'],
                             extra_compile_args=options['cflags'],
                             extra_link_args=options['ldflags'],
                             language='c++'))
    return cythonize(out)


def print_epilogue():
    """Display a post-setup epilogue."""
    longbar = "="*80
    message = "\n".join([
        longbar,
        "Installation complete",
        "Please cite QuTiP in your publication.",
        longbar,
        "For your convenience a BibTeX reference can be easily generated with",
        "`qutip.cite()`",
    ])
    print(message)


if __name__ == "__main__":
    options = process_options()
    create_version_py_file(options)
    extensions = create_extension_modules(options)
    # Most of the kwargs to setup are defined in setup.cfg; the only ones we
    # keep here are ones that we have done some compile-time processing on.
    setup(
        version=options['version'],
        ext_modules=extensions,
        cmdclass={'build_ext': build_ext},
    )
    print_epilogue()
