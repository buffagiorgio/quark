import logging
import json
import os
from os.path import exists, join, isdir
from shutil import rmtree
from subprocess import check_output, call, PIPE, Popen, check_call, CalledProcessError
from urllib.parse import urlparse
import shutil

import xml.etree.ElementTree as ElementTree

from quark.utils import DirectoryContext, fork, SubprocessContext
from quark.utils import freeze_file, dependency_file, mkdir, load_conf, walk_tree

logger = logging.getLogger(__name__)

class QuarkError(RuntimeError):
    pass

class Node:
    def __init__(self):
        self.parents = set()
        self.children = set()

class Subproject(Node):
    subproject_dir = None

    @staticmethod
    def _parse_fragment(url):
        res = {}
        for equality in url.fragment.split():
            index = equality.find('=')
            key = equality[:index]
            value = equality[index + 1:]
            res[key] = value
        return res

    @staticmethod
    def create(name, urlstring, directory, options, **kwargs):
        if not urlstring:
            if exists(join(directory, ".svn")):
                urlstring = SvnSubproject.url_from_directory(directory)
                url = urlparse(urlstring)
                res = SvnSubproject(name, url, directory, options, **kwargs)
            elif exists(join(directory, ".git")):
                urlstring = GitSubproject.url_from_directory(directory)
                url = urlparse(urlstring)
                res = GitSubproject(name, url, directory, options, **kwargs)
            else:
                raise QuarkError("Couldn't detect repository type for directory %s" % directory)
        else:
            url = urlparse(urlstring)
            args = (name, url, directory, options)
            if url.scheme.startswith('git'):
                res = GitSubproject(*args, **kwargs)
            elif url.scheme.startswith('svn'):
                res = SvnSubproject(*args, **kwargs)
            else:
                raise ValueError("Unrecognized dependency for url '%s'", urlstring)
        res.urlstring = urlstring
        return res

    @staticmethod
    def create_dependency_tree(source_dir, url=None, options=None, update=False):
        root = Subproject.create("root", url, source_dir, {}, toplevel = True)
        if url and update:
            root.checkout()
        conf = load_conf(source_dir)
        subproject_dir = join(source_dir, conf.get("subprojects_dir", 'lib'))
        if conf is None:
            return root, {}
        stack = [root]
        modules = {}

        def get_option(key):
            try:
                return root.options[key]
            except KeyError as e:
                err = e
            for module in modules.values():
                try:
                    return module.options[key]
                except KeyError as e:
                    err = e
            raise err

        def add_module(parent, name, uri, options, **kwargs):
            newmodule = Subproject.create(name, uri, join(subproject_dir, name), options, **kwargs)
            mod = modules.setdefault(name, newmodule)
            if mod is newmodule:
                mod.parents.add(parent)
                if update:
                    mod.update()
            else:
                if newmodule.exclude_from_cmake != mod.exclude_from_cmake:
                    children_conf = [join(parent.directory, dependency_file) for parent in mod.parents]
                    parent_conf = join(parent.directory, dependency_file)
                    raise ValueError("Conflicting value of 'exclude_from_cmake'"
                                     " attribute for module '%s': '%s' required by %s and %s required by %s" %
                                     (name, str(mod.exclude_from_cmake), children_conf, str(parent.exclude_from_cmake),
                                      parent_conf)
                                     )
                if not newmodule.same_checkout(mod) and uri is not None:
                    children = [join(parent.directory, dependency_file) for parent in mod.parents]
                    parent = join(parent.directory, dependency_file)
                    raise ValueError(
                        "Conflicting URLs for module '%s': '%s' required by %s and '%s' required by '%s'" %
                        (name,
                         mod.urlstring, children,
                         newmodule.urlstring, parent))

                else:
                    for key, value in options.items():
                        mod.options.setdefault(key, value)
                        if mod.options[key] != value:
                            raise ValueError(
                                "Conflicting values option '%s' of module '%s'" % (key, mod.name)
                            )
            stack.append(mod)
            parent.children.add(mod)

        freeze_conf = join(root.directory, freeze_file)
        if exists(freeze_conf):
            with open(freeze_conf, 'r') as f:
                freeze_dict = json.load(f)
        else:
            freeze_dict = {}
        if update:
            mkdir(subproject_dir)
        while len(stack):
            current_module = stack.pop()
            if current_module.external_project:
                generate_cmake_script(current_module.directory, update = update)
                continue
            conf = load_conf(current_module.directory)
            if conf:
                if current_module.toplevel:
                    current_module.options = conf.get('toplevel_options', {})
                    if options:
                        current_module.options.update(options)
                for name, depobject in conf.get('depends', {}).items():
                    external_project = depobject.get('external_project', False)
                    add_module(current_module, name,
                               freeze_dict.get(name, depobject.get('url', None)), depobject.get('options', {}),
                               exclude_from_cmake=depobject.get('exclude_from_cmake', external_project),
                               external_project=external_project
                               )
                for key, optobjects in conf.get('optdepends', {}).items():
                    if isinstance(optobjects, dict):
                        optobjects = [optobjects]
                    for optobject in optobjects:
                        try:
                            value = get_option(key)
                        except KeyError:
                            continue
                        if value == optobject['value']:
                            for name, depobject in optobject['depends'].items():
                                add_module(current_module, name,
                                           freeze_dict.get(name, depobject.get('url', None)),
                                           depobject.get('options', {}))
        return root, modules

    def __init__(self, name=None, directory=None, options=None, exclude_from_cmake=False, external_project=False, toplevel = False):
        super().__init__()
        self.name = name
        self.directory = directory
        self.options = options or {}
        self.exclude_from_cmake = exclude_from_cmake
        self.external_project = external_project
        self.toplevel = toplevel

    def __hash__(self):
        return self.name.__hash__()

    def same_checkout(self, other):
        return True

    def checkout(self):
        raise NotImplementedError()

    def update(self):
        raise NotImplementedError()

    def status(self):
        print("Unsupported external %s" % self.directory)

    def local_edit(self):
        raise NotImplementedError()

    def url_from_checkout(self):
        raise NotImplementedError()

    def mirror(self, dest):
        raise NotImplementedError()

    def toJSON(self):
        return {
            "name": self.name,
            "children": [child.toJSON() for child in self.children],
            "options": self.options,
        }


class GitSubproject(Subproject):
    def __init__(self, name, url, directory, options, **kwargs):
        super().__init__(name, directory, options, **kwargs)
        self.ref = 'origin/HEAD'
        if url.fragment:
            fragment = Subproject._parse_fragment(url)
            if 'commit' in fragment:
                self.ref = fragment['commit']
            elif 'tag' in fragment:
                self.ref = fragment['tag']
            elif 'branch' in fragment:
                self.ref = 'origin/%s' % fragment['branch']
        self.url = url._replace(fragment='')._replace(scheme=url.scheme.replace('git+', ''))

    def same_checkout(self, other):
        if isinstance(other, GitSubproject) and (self.url, self.ref) == (other.url, other.ref):
            return True
        return False

    def check_origin(self):
        with DirectoryContext(self.directory):
            if check_output(['git', 'config', '--get', 'remote.origin.url']) != self.url:
                if not self.has_local_edit():
                    logger.warning("%s is not a clone of %s "
                                   "but it hasn't local modifications, "
                                   "removing it..", self.directory, self.url.geturl())
                    rmtree(self.directory)
                    self.checkout()
                else:
                    raise ValueError(
                        "'%s' is not a clone of '%s' and has local"
                        " modifications, I don't know what to do with it..." %
                        self.directory, self.url.geturl())

    def checkout(self):
        fork(['git', 'clone', self.url.geturl(), self.directory])

    def update(self):
        if not exists(self.directory):
            self.checkout()
        elif self.has_local_edit():
            logger.warning("Directory '%s' contains local modifications" % self.directory)
        else:
            with DirectoryContext(self.directory):
                fork(['git', 'fetch'])
                fork(['git', 'checkout', self.ref])

    def status(self):
        fork(['git', "--git-dir=%s/.git" % self.directory, "--work-tree=%s" % self.directory, 'status'])

    def has_local_edit(self):
        with DirectoryContext(self.directory):
            cmd = ['git', 'status', '--porcelain']
            with SubprocessContext(cmd, universal_newlines=True, stdout=PIPE, check=True) as pipe:
                for _ in pipe.stdout:
                    return True
        return False

    @staticmethod
    def url_from_directory(directory):
        with DirectoryContext(directory):
            with SubprocessContext(['git', 'remote', 'get-url', 'origin'], universal_newlines=True, stdout=PIPE,
                                   check=True) as pipe:
                origin = pipe.stdout.read()[:-1]
            with SubprocessContext(['git', 'log', '-1', '--format=%H'], universal_newlines=True, stdout=PIPE,
                                   check=True) as pipe:
                commit = pipe.stdout.read()[:-1]
        return 'git+%s#commit=%s' % (origin, commit)

    def url_from_checkout(self):
        return self.url_from_directory(self.directory)

    def mirror(self, dst_dir):
        source_dir = self.directory
        def mkdir_p(path):
            if path.strip() != '' and not os.path.exists(path):
                os.makedirs(path)

        env = os.environ.copy()
        env['LC_MESSAGES'] = 'C'

        def tracked_files():
            p = Popen(['git', 'ls-tree', '-r', '--name-only', 'HEAD'], stdout=PIPE, env=env)
            out = p.communicate()[0]
            if p.returncode != 0 or not out.strip():
                return None
            return [e.strip() for e in out.splitlines() if os.path.exists(e)]

        def cp(src, dst):
            r, f = os.path.split(dst)
            mkdir_p(r)
            shutil.copy2(src, dst)

        with DirectoryContext(source_dir):
            for t in tracked_files():
                cp(t, os.path.join(dst_dir, t.decode()))

class SvnSubproject(Subproject):
    def __init__(self, name, url, directory, options, **kwargs):
        super().__init__(name, directory, options, **kwargs)
        self.rev = 'HEAD'
        fragment = (url.fragment and Subproject._parse_fragment(url)) or {}
        rev = fragment.get('rev', None)
        branch = fragment.get('branch', None)
        tag = fragment.get('tag', None)
        if (branch or tag) and self.url.path.endswith('trunk'):
            url = url._replace(path=self.url.path[:-5])
        if branch:
            url = url._replace(path=join(url.path, 'branches', branch))
        elif tag:
            url = url._replace(path=join(url.path, 'tags', tag))
        if rev:
            url = url._replace(path=url.path + '@' + rev)
            self.rev = rev
        self.url = url._replace(fragment='')

    def same_checkout(self, other):
        if isinstance(other, SvnSubproject) and (self.url, self.rev) == (other.url, other.rev):
            return True
        return False

    def checkout(self):
        fork(['svn', 'checkout', self.url.geturl(), self.directory])

    def update(self):
        if not exists(self.directory):
            self.checkout()
        elif self.has_local_edit():
            logger.warning("Directory '%s' contains local modifications" % self.directory)
        else:
            with DirectoryContext(self.directory):
                fork(['svn', 'switch', self.url.geturl()])
                # fork(['svn', 'up', '-r', self.rev])

    def status(self):
        fork(['svn', 'status', self.directory])

    def has_local_edit(self):
        with SubprocessContext(['svn', 'st', '--xml', self.directory], universal_newlines=True, stdout=PIPE,
                               check=True) as pipe:
            doc = ElementTree.parse(pipe.stdout)
        for entry in doc.findall('./status/target/entry[@path="%s"]/entry[@item="modified"]' % self.directory):
            return True
        return False

    @staticmethod
    def url_from_directory(directory):
        with SubprocessContext(['svn', 'info', '--xml', directory], universal_newlines=True, stdout=PIPE,
                               check=True) as pipe:
            doc = ElementTree.parse(pipe.stdout)
        return doc.findall('./entry/url')[0].text + "@" + doc.findall('./entry/commit')[0].get('revision')

    def url_from_checkout(self):
        return self.url_from_directory(self.directory)

    def mirror(self, dst, quick = False):
        import shutil
        src = self.directory

        os.chdir(src)
        if not quick and isdir(dst):
            shutil.rmtree(dst)
        if not isdir(dst):
            os.makedirs(dst)

        # Forziamo il locale a inglese, perché parseremo l'output di svn e non
        # vogliamo errori dovuti alle traduzioni.
        env = os.environ.copy()
        env["LC_MESSAGES"] = "C"

        dirs = ["."]

        # Esegue svn info ricorsivamente per iterare su tutti i file versionati.
        for D in dirs:
            infos = {}
            for L in Popen(["svn", "info", "--recursive", D], stdout=PIPE, env=env).stdout:
                L = L.decode()
                if L.strip():
                    k,v = L.strip().split(": ", 1)
                    infos[k] = v
                else:
                    if infos["Schedule"] == "delete":
                        continue
                    fn = infos["Path"]
                    infos = {}
                    if fn == ".":
                        continue
                    fn1 = join(src, fn)
                    fn2 = join(dst, fn)
                    if isdir(fn1):
                        if not isdir(fn2):
                            os.makedirs(fn2)
                    elif not quick or newer(fn1, fn2):
                        shutil.copy2(fn1, fn2)

def generate_cmake_script(source_dir, url=None, options=None, print_tree=False,update=True):
    root, modules = Subproject.create_dependency_tree(source_dir, url, options, update=update)
    conf = load_conf(source_dir)
    subproject_dir = join(source_dir, conf.get("subprojects_dir", 'lib'))
    if print_tree:
        print(json.dumps(root.toJSON(), indent=4))
    if update:
        with open(join(subproject_dir, 'CMakeLists.txt'), 'w') as cmake_lists_txt:
            processed = set()

            def dump_options(module):
                for key, value in module.options.items():
                    if value is None:
                        cmake_lists_txt.write('unset(%s CACHE)\n' % (key))
                        continue
                    elif isinstance(value, bool):
                        kind = "BOOL"
                        value = 'ON' if value else 'OFF'
                    else:
                        kind = "STRING"
                    cmake_lists_txt.write('set(%s %s CACHE INTERNAL "" FORCE)\n' % (key, value))

            def cb(module):
                if (module is root or
                    module.name in processed or 
                    module.exclude_from_cmake or 
                    not exists(join(module.directory, "CMakeLists.txt"))):
                    return
                dump_options(module)
                cmake_lists_txt.write('add_subdirectory(%s)\n' % (module.directory))
                processed.add(module.name)
            dump_options(root)
            walk_tree(root, cb)
