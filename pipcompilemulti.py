#!/usr/bin/env python
"""
Build locked requirements files for each of:
    base.in
    test.in
    local.in

External dependencies are hard-pinned using ==
Internal dependencies are soft-pinned using ~=
".post23423" version postfixes are truncated
"""

import os
import re
import sys
import glob
import hashlib
import logging
import itertools
import subprocess
from fnmatch import fnmatch

import click
from toposort import toposort_flatten


__author__ = 'Peter Demin'
__email__ = 'peterdemin@gmail.com'
__version__ = '1.1.8'


logger = logging.getLogger("pip-compile-multi")

DEFAULT_HEADER = """
#
# This file is autogenerated by pip-compile-multi
# To update, run:
#
#    pip-compile-multi
#
""".lstrip()


OPTIONS = {
    'compatible_patterns': [],
    'base_dir': 'requirements',
    'allow_post': ['test', 'local'],
    'in_ext': 'in',
    'out_ext': 'txt',
    'header_file': None,
}


@click.group(invoke_without_command=True)
@click.pass_context
@click.option('--compatible', '-c', multiple=True,
              help='Glob expression for packages with compatible (~=) '
                   'version constraint')
@click.option('--post', '-p', multiple=True,
              help='Environment name (base, test, etc) that can have '
                   'packages with post-release versions (1.2.3.post777)')
@click.option('--directory', '-d', default=OPTIONS['base_dir'],
              help='Directory path with requirements files')
@click.option('--in-ext', '-i', default=OPTIONS['in_ext'],
              help='File extension of input files')
@click.option('--out-ext', '-o', default=OPTIONS['out_ext'],
              help='File extension of output files')
@click.option('--header', '-h', default='',
              help='File path with custom header text for generated files')
def cli(ctx, compatible, post, directory, in_ext, out_ext, header):
    """Recompile"""
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    OPTIONS.update({
        'compatible_patterns': compatible,
        'allow_post': set(post),
        'base_dir': directory,
        'in_ext': in_ext,
        'out_ext': out_ext,
        'header_file': header or None,
    })
    if ctx.invoked_subcommand is None:
        recompile()


def recompile():
    """
    Compile requirements files for all environments.
    """
    pinned_packages = {}
    env_confs = discover(
        os.path.join(
            OPTIONS['base_dir'],
            '*.' + OPTIONS['in_ext'],
        )
    )
    if OPTIONS['header_file']:
        with open(OPTIONS['header_file']) as fp:
            base_header_text = fp.read()
    else:
        base_header_text = DEFAULT_HEADER
    for conf in env_confs:
        rrefs = recursive_refs(env_confs, conf['name'])
        env = Environment(
            name=conf['name'],
            ignore=merged_packages(pinned_packages, rrefs),
            allow_post=conf['name'] in OPTIONS['allow_post'],
        )
        logger.info("Locking %s to %s. References: %r",
                    env.infile, env.outfile, sorted(rrefs))
        env.create_lockfile()
        header_text = generate_hash_comment(env.infile) + base_header_text
        env.replace_header(header_text)
        env.add_references(conf['refs'])
        pinned_packages[conf['name']] = set(env.packages)


def merged_packages(env_packages, names):
    """
    Return union set of environment packages with given names

    >>> sorted(merged_packages(
    ...     {
    ...         'a': {1, 2},
    ...         'b': {2, 3},
    ...         'c': {3, 4}
    ...     },
    ...     ['a', 'b']
    ... ))
    [1, 2, 3]
    """
    ignored_sets = [
        env_packages[name]
        for name in names
    ]
    if ignored_sets:
        return set.union(*ignored_sets)
    return set()


def recursive_refs(envs, name):
    """
    Return set of recursive refs for given env name

    >>> local_refs = sorted(recursive_refs([
    ...     {'name': 'base', 'refs': []},
    ...     {'name': 'test', 'refs': ['base']},
    ...     {'name': 'local', 'refs': ['test']},
    ... ], 'local'))
    >>> local_refs == ['base', 'test']
    True
    """
    refs_by_name = {
        env['name']: set(env['refs'])
        for env in envs
    }
    refs = refs_by_name[name]
    if refs:
        indirect_refs = set(itertools.chain.from_iterable([
            recursive_refs(envs, ref)
            for ref in refs
        ]))
    else:
        indirect_refs = set()
    return set.union(refs, indirect_refs)


class Environment(object):
    """requirements file"""

    IN_EXT = '.in'
    OUT_EXT = '.txt'
    RE_REF = re.compile('^(?:-r|--requirement)\s*(?P<path>\S+).*$')
    PY3_IGNORE = set(['future', 'futures'])  # future[s] are obsolete in python3

    def __init__(self, name, ignore=None, allow_post=False):
        """
        name - name of the environment, e.g. base, test
        ignore - set of package names to omit in output
        """
        self.name = name
        self.ignore = set(ignore or [])
        if sys.version_info[0] >= 3:
            self.ignore.update(self.PY3_IGNORE)
        self.allow_post = allow_post
        self.packages = set()

    def create_lockfile(self):
        """
        Write recursive dependencies list to outfile
        with hard-pinned versions.
        Then fix it.
        """
        process = subprocess.Popen(
            self.pin_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = process.communicate()
        if process.returncode == 0:
            self.fix_lockfile()
        else:
            logger.critical("ERROR executing %s", ' '.join(self.pin_command))
            logger.critical("Exit code: %s", process.returncode)
            logger.critical(stdout.decode('utf-8'))
            logger.critical(stderr.decode('utf-8'))
            raise RuntimeError("Failed to pip-compile {0}".format(self.infile))

    @classmethod
    def parse_references(cls, filename):
        """
        Read filename line by line searching for pattern:

        -r file.in
        or
        --requirement file.in

        return set of matched file names without extension.
        E.g. ['file']
        """
        references = set()
        for line in open(filename):
            matched = cls.RE_REF.match(line)
            if matched:
                reference = matched.group('path')
                reference_base = os.path.splitext(reference)[0]
                references.add(reference_base)
        return references

    @property
    def infile(self):
        """Path of the input file"""
        return os.path.join(OPTIONS['base_dir'],
                            '{0}{1}'.format(self.name, self.IN_EXT))

    @property
    def outfile(self):
        """Path of the output file"""
        return os.path.join(OPTIONS['base_dir'],
                            '{0}{1}'.format(self.name, self.OUT_EXT))

    @property
    def pin_command(self):
        """Compose pip-compile shell command"""
        return [
            'pip-compile',
            '--verbose',
            '--rebuild',
            '--upgrade',
            '--no-index',
            '--output-file', self.outfile,
            self.infile,
        ]

    def fix_lockfile(self):
        """
        Run each line of outfile through fix_pin
        """
        with open(self.outfile, 'rt') as fp:
            lines = [
                self.fix_pin(line)
                for line in fp
            ]
        with open(self.outfile, 'wt') as fp:
            fp.writelines([
                line + '\n'
                for line in lines
                if line is not None
            ])

    def fix_pin(self, line):
        """
        Fix dependency by removing post-releases from versions
        and loosing constraints on internal packages.
        Drop packages from ignore set

        Also populate packages set
        """
        dep = Dependency(line)
        if dep.valid:
            if dep.package in self.ignore:
                return None
            self.packages.add(dep.package)
            if not self.allow_post or dep.is_compatible:
                # Always drop post for internal packages
                dep.drop_post()
            return dep.serialize()
        return line.strip()

    def add_references(self, other_names):
        """Add references to other_names in outfile"""
        if not other_names:
            # Skip on empty list
            return
        with open(self.outfile, 'rt') as fp:
            header, body = self.split_header(fp)
        with open(self.outfile, 'wt') as fp:
            fp.writelines(header)
            for other_name in sorted(other_names):
                ref = other_name + self.OUT_EXT
                fp.write('-r {0}\n'.format(ref))
            fp.writelines(body)

    @staticmethod
    def split_header(fp):
        """
        Read file pointer and return pair of lines lists:
        first - header, second - the rest.
        """
        body_start, header_ended = 0, False
        lines = []
        for line in fp:
            if line.startswith('#') and not header_ended:
                # Header text
                body_start += 1
            else:
                header_ended = True
            lines.append(line)
        return lines[:body_start], lines[body_start:]

    def replace_header(self, header_text):
        """Replace pip-compile header with custom text"""
        with open(self.outfile, 'rt') as fp:
            _, body = self.split_header(fp)
        with open(self.outfile, 'wt') as fp:
            fp.write(header_text)
            fp.writelines(body)


class Dependency(object):
    """Single dependency line"""

    COMMENT_JUSTIFICATION = 26

    # Example:
    # unidecode==0.4.21         # via myapp
    # [package]  [version]      [comment]
    RE_DEPENDENCY = re.compile(
        r'(?iu)(?P<package>.+)'
        r'=='
        r'(?P<version>[^ ]+)'
        r' *'
        r'(?:(?P<comment>#.*))?$'
    )
    RE_EDITABLE_FLAG = re.compile(
        r'^-e '
    )

    def __init__(self, line):
        m = self.RE_DEPENDENCY.match(line)
        if m:
            self.valid = True
            self.package = m.group('package')
            self.version = m.group('version').strip()
            self.comment = (m.group('comment') or '').strip()
        else:
            self.valid = False

    def serialize(self):
        """
        Render dependency back in string using:
            ~= if package is internal
            == otherwise
        """
        equal = '~=' if self.is_compatible else '=='
        package_version = '{package}{equal}{version}  '.format(
            package=self.without_editable(self.package),
            version=self.version,
            equal=equal,
        )
        return '{0}{1}'.format(
            package_version.ljust(self.COMMENT_JUSTIFICATION),
            self.comment,
        ).rstrip()

    @classmethod
    def without_editable(cls, line):
        """
        Remove the editable flag.
        It's there because pip-compile can't yet do without it
        (see https://github.com/jazzband/pip-tools/issues/272 upstream),
        but in the output of pip-compile it's no longer needed.
        """
        return cls.RE_EDITABLE_FLAG.sub('', line)

    @property
    def is_compatible(self):
        """Check if package name is matched by compatible_patterns"""
        for pattern in OPTIONS['compatible_patterns']:
            if fnmatch(self.package.lower(), pattern):
                return True
        return False

    def drop_post(self):
        post_index = self.version.find('.post')
        if post_index >= 0:
            self.version = self.version[:post_index]


def discover(glob_pattern):
    """
    Find all files matching given glob_pattern,
    parse them, and return list of environments:

    >>> envs = discover("requirements/*.in")
    >>> envs == [
    ...     {'name': 'base', 'refs': set()},
    ...     {'name': 'test', 'refs': {'base'}},
    ...     {'name': 'local', 'refs': {'test'}},
    ... ]
    True
    """
    in_paths = glob.glob(glob_pattern)
    names = {
        extract_env_name(path): path
        for path in in_paths
    }
    return order_by_refs([
        {'name': name, 'refs': Environment.parse_references(in_path)}
        for name, in_path in names.items()
    ])


def extract_env_name(file_path):
    """Return environment name for given requirements file path"""
    return os.path.splitext(os.path.basename(file_path))[0]


def order_by_refs(envs):
    """
    Return topologicaly sorted list of environments.
    I.e. all referenced environments are placed before their references.
    """
    topology = {
        env['name']: set(env['refs'])
        for env in envs
    }
    by_name = {
        env['name']: env
        for env in envs
    }
    return [
        by_name[name]
        for name in toposort_flatten(topology)
    ]


@cli.command()
@click.pass_context
def verify(ctx):
    """
    For each environment verify hash comments and report failures.
    If any failure occured, exit with code 1.
    """
    env_confs = discover(
        os.path.join(
            OPTIONS['base_dir'],
            '*.' + OPTIONS['in_ext'],
        )
    )
    success = True
    for conf in env_confs:
        env = Environment(name=conf['name'])
        logger.info("Verifying that %s was generated from %s.",
                    env.outfile, env.infile)
        current_comment = generate_hash_comment(env.infile)
        existing_comment = parse_hash_comment(env.outfile)
        if current_comment == existing_comment:
            logger.info("Success - comments match.")
        else:
            logger.error("FAILURE!")
            logger.error("Expecting: %s", current_comment.strip())
            logger.error("Found:     %s", existing_comment.strip())
            success = False
    if not success:
        ctx.exit(1)


def generate_hash_comment(file_path):
    """
    Read file with given file_path and return string of format

        # SHA1:da39a3ee5e6b4b0d3255bfef95601890afd80709

    which is hex representation of SHA1 file content hash
    """
    with open(file_path, 'rb') as fp:
        hexdigest = hashlib.sha1(fp.read().strip()).hexdigest()
    return "# SHA1:{0}\n".format(hexdigest)


def parse_hash_comment(file_path):
    """
    Read file with given file_path line by line,
    return the first line that starts with "# SHA1:", like this:

        # SHA1:da39a3ee5e6b4b0d3255bfef95601890afd80709
    """
    with open(file_path) as fp:
        for line in fp:
            if line.startswith("# SHA1:"):
                return line
    return None
