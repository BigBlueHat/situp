#! /usr/bin/env python
import base64
import codecs
import json
import os
import re
import sys
import stat
import string
import logging
import urllib
import urllib2
import tarfile
import zipfile
import shutil
import uuid
import mimetypes
import getpass
from optparse import OptionParser, OptionGroup
from collections import defaultdict, namedtuple
from urlparse import urlunparse, urlparse
from httplib import HTTPConnection
from httplib import HTTPSConnection
from httplib import HTTPException
from fnmatch import fnmatch

CAN_MINIFY_JS = False

try:
    from minify import jsmin
    CAN_MINIFY_JS = True
except:
    pass

CAN_PREPROC = False
try:
    import preproc
    CAN_PREPROC = True
except:
    pass

CAN_POSTPROC = False
try:
    import postproc
    CAN_POSTPROC = True
except:
    pass

__version__ = "0.2.1"

if os.name == 'nt':
    def _replace_backslash(name):
        return name.replace("\\", "/")
else:
    def _replace_backslash(name):
        return name


class CommandDispatch:
    def __init__(self):
        self.commands = {}
        self.default = ''

    def register_command(self, command, default=False):
        self.commands[command.name.lower()] = command
        if default:
            self.default = command.name.lower()

    def __call__(self, command=False):
        if command:
            self.commands[command]()
        else:
            self.default_command()

    def default_command(self):
        if self.default:
            self.commands[self.default]()
        else:
            usage = '%prog command [options]\n'
            usage += 'Available commands: '
            usage += ' '.join(sorted(self.commands.keys()))
            self.parser = OptionParser(usage=usage, version=__version__)
            self.parser.parse_args()
            self.parser.print_help()


class Command:
    """
    A command has a name, an option parser and a dictionary of sub commands it
    can call.
    """
    name = "interface"
    no_required_args = 0
    required_opts = []
    dependencies = []
    usage = "usage: %prog [options] COMMAND [options] [args]"

    def __init__(self):
        """
        Initialise the logger and OptionParser for the Command.
        """
        logging.basicConfig()
        self.logger = logging.getLogger('situp-%s' % self.name)
        self.logger.setLevel(logging.DEBUG)

        # Need to deal with competing OptionParsers...
        self.parser = OptionParser(conflict_handler="resolve")
        self.parser.set_usage(self.usage)

        self.parser.epilog = " ".join(str(self.__doc__).split())
        self._default_options()
        self._add_options()

    def __call__(self):
        """
        Set up the logger, work out if I should print help or call the command.
        """
        (options, args) = self._process_args()

        self._configure_logger(options)

        self.logger.debug('called')
        self.logger.debug(args)
        self.logger.debug(options)

        self.run_command(args, options)

    def run_command(self, args=None, options=None):
        raise NotImplementedError('Not implemented in base class')

    def _process_args(self):
        """
        Process the option parser, updating it with data from parent parser
        then check the args are valid.
        """
        (options, args) = self.parser.parse_args()

        die = False
        for option in self.required_opts:
            if options.ensure_value(option, 'NOTSET') == 'NOTSET':
                print '%s is a required option and not set' % option
                die = True
        if die:
            print 'Run command with -h/--help for further information'
            sys.exit(1)
        return options, args[1:]

    def _configure_logger(self, options):
        if options.quiet:
            self.logger.setLevel(logging.WARNING)
        if options.silent:
            self.logger.setLevel(logging.CRITICAL)
        elif options.debug:
            self.logger.setLevel(logging.DEBUG)
        else:
            self.logger.setLevel(logging.INFO)

    def _add_options(self):
        """
        Add options to the command's option parser
        """
        pass

    def _default_options(self):
        group = OptionGroup(self.parser, "Base options", "")

        group.add_option("--quiet",
                action="store_true", dest="quiet", default=False,
                help="reduce messages going to stdout")

        group.add_option("--debug",
                action="store_true", dest="debug", default=False,
                help="print extra messages to stdout")

        group.add_option("--version",
                action="store_true", dest="version", default=False,
                help="print situp.py version and exit")

        group.add_option("--silent",
                action="store_true", dest="silent", default=False,
                help="print as little as possible to stdout")

        self.parser.add_option_group(group)

        group = OptionGroup(self.parser, "Situp options", "Situp allows you to"
                    " have multiple design documents in your application via"
                    " the -d/--design switch. You can work on your app in"
                    " another directory by specifying -r/--root"
        )
        group.add_option("-d", "--design",
                    metavar="DESIGN",
                    dest="design",
                    default=['_design'],
                    action='append',
                    help="modify the design document DESIGN")

        pwd = os.getcwd()
        group.add_option("-r", "--root",
                    dest="root", default=pwd,
                    help="Application root directory, default is %s" % pwd)

        self.parser.add_option_group(group)


LocatedFile = namedtuple('LocatedFile', ['path', 'filename'])


class AddServer(Command):
    """
    Add a server to the servers.json file
    """
    name = 'addserver'
    required_opts = ['name', 'server']

    def _add_options(self):
        """
        Give the OptionParser additional options
        """
        self.parser.add_option("-s", "--server",
                dest="server",
                help="The server to add [required]")
        self.parser.add_option("-n", "--name",
                dest="name",
                help="The simple name server to add [required]")

    def _process_args(self, args=None, options=None):
        options, args = Command._process_args(self)
        msg = 'Username for server, press enter for no user/password auth:'
        username = raw_input(msg)
        if username:
            options.auth_string = "%s" % base64.encodestring('%s:%s' % (
                                          username, getpass.getpass())).strip()
        return options, args

    def run_command(self, args, options):
        servers = {}
        if os.path.exists('servers.json'):
            f = open('servers.json')
            servers = json.load(f)
            f.close()
        servers[options.name] = {'url': options.server}
        if options.ensure_value('auth_string', False):
            servers[options.name]['auth'] = options.auth_string
        f = open('servers.json', 'w')
        json.dump(servers, f)
        f.close()


class Push(Command):
    """
    The Push command sends the application to the CouchDB server. Specify a
    design to push only a single design document, otherwise all designs in the
    app will be pushed.
    """
    name = 'push'
    no_required_args = 0
    # TODO: pick this up from config
    # TODO: add a --no-push-docs option
    ignored_files = ['.DS_Store', '.cvs', '.svn', '.hg', '.git', '*.swp']

    def _add_options(self):
        """
        Give the OptionParser additional options
        """
        about = "Options available to the push command."
        group = OptionGroup(self.parser, "Push options", about)
        group.add_option("-o", "--open",
                dest="open_app",
                action="store_true", default=False,
                help="Once pushed, open the application")
        group.add_option("-s", "--server",
                dest="servers", default=[], action='append',
                help="Push the app to servers (multiple -s options allowed)")
        group.add_option('-e', '--database', dest='database',
                help="Push the app to named database")
        group.add_option('-o', '--docs',
                dest='only_docs', default=False, action="store_true",
                help="Push only the _docs directory")

        if CAN_PREPROC:
            proc_l = lambda x: not x.startswith('_')
            pre_help = "Run named preprocessors, available preprocessors"
            pre_help += " %s " % ', '.join(filter(proc_l, dir(preproc)))
            pre_help += "(multiple allowed)"

            group.add_option("--pre",
                    dest="preproc", default=[], action='append',
                    help=pre_help)

        if CAN_POSTPROC:
            post_help = "Run named postprocessors, available postprocessors"
            post_help +=" %s " % ', '.join(filter(proc_l, dir(postproc)))
            post_help += "(multiple allowed)"

            group.add_option("--post",
                    dest="postproc", default=[], action='append',
                    help=post_help)

        if CAN_MINIFY_JS:
            group.add_option("-m", "--minify",
                dest="minify", default=False, action="store_true",
                help="Minify javascript before pushing to database")

        self.parser.add_option_group(group)

    def __call__(self):
        """
        Set up the logger, work out if I should print help or call the command.
        """
        (options, args) = self._process_args()

        self._configure_logger(options)

        self.logger.debug('called')
        self.logger.debug(args)
        self.logger.debug(options)

        if CAN_PREPROC:
            for pre in set(options.preproc):
                self.logger.debug('running %s' % pre)
                getattr(preproc, pre)(args, options, self.logger)

        self.run_command(args, options)

        if CAN_POSTPROC:
            for post in set(options.postproc):
                self.logger.debug('running %s' % post)
                getattr(postproc, post)(args, options, self.logger)


    def _push_docs(self, docs_list, db, servers):
        """
        Push dictionaries into json docs in the server
        TODO: spin off into a worker thread
        """
        for server in servers.keys():
            srv = servers[server]
            self.logger.info('upload to %s (%s/%s)' % (server, srv['url'], db))
            data = {}
            try:
                def request(server, method, url, auth=False):
                    conn = None
                    if server.startswith('https://'):
                        conn = HTTPSConnection(server.strip('https://'))
                    else:
                        conn = HTTPConnection(server.strip('http://'))
                    conn.putrequest(method, url)
                    if auth:
                        conn.putheader("Authorization", "Basic %s" % auth)
                    conn.putheader("User-Agent", "situp-%s" % __version__)
                    conn.endheaders()
                    response = conn.getresponse()
                    conn.close()
                    return response

                request(srv['url'], 'PUT', "/%s" % db, srv.get('auth', False))

                for doc in docs_list:
                    if '_id' in doc.keys():
                        docid = doc['_id']
                        # HEAD the doc
                        url = "/%s/%s" % (db, docid)
                        auth = srv.get('auth', False)
                        head = request(srv['url'], 'HEAD', url, auth)
                        # get its _rev, append _rev to the doc dict
                        if head.getheader('etag', False):
                            etag = head.getheader('etag', False)
                            doc['_rev'] = etag.replace('"', '')

                req = urllib2.Request('%s/%s/_bulk_docs' % (srv['url'], db))
                req.add_header("Content-Type", "application/json")
                req.add_header("User-Agent", "situp-%s" % __version__)
                if 'auth' in srv.keys():
                    req.add_header("Authorization", "Basic %s" % srv['auth'])
                data = {'docs': docs_list}
                req.add_data(json.dumps(data))
                f = urllib2.urlopen(req)
                self.logger.info(f.read())
            except Exception, e:
                self.logger.error("upload to %s failed" % server)
                self.logger.info(e)

    def _allowed_file(self, filepath):
        """
        Check that filepath isn't in self.ignored_files, return True if the
        file is allowed.
        """
        return True not in [fnmatch(filepath, r) for r in self.ignored_files]

    def _attach(self, afile, file_path, minify=False):
        """
        Takes a path to a file, and the name of the attachment, works out it's
        mime type (assumes text/plain if it can't be determined) and returns
        the necessary dict to upload to a doc.
        """
        mimetypes.init(files=[])
        mime = mimetypes.guess_type(file_path)[0]

        if not mime:
            msg = 'Assuming text/plain mime type for %s'
            self.logger.warning(msg % file_path)
            mime = 'text/plain'

        if minify and mime == "application/javascript":
            data = self._minify(file_path)
        else:
            f = open(os.path.join(file_path), 'rb')
            data = base64.b64encode(f.read())
            f.close()

        return {afile: {
                'data': data,
                'content_type': mime
                }}

    def _walk_design(self, name, design, options):
        """
        Walk through the design document, building a dictionary as it goes.
        """

        def nest(path_dict, path_elem):
            """
            Build the required nested data structure
            """
            if self._allowed_file(path_elem):
                return {path_elem: path_dict}

        def recurse_update(a_dict, b_dict):
            try:
                for k, v in b_dict.items():
                    if k not in a_dict.keys() or type(v) != type(a_dict[k]):
                        a_dict[k] = v
                    else:
                        a_dict[k] = recurse_update(a_dict[k], v)
            except:
                print 'skipping %s' % b_dict
            return a_dict

        attachments = {}
        app = {'_id': name}
        for root, dirs, files in os.walk(design):
            path = root.split(name)[1].split('/')[1:]
            dirs = filter(self._allowed_file, dirs)
            if files:
                d = {}
                for afile in filter(self._allowed_file, files):
                    afile_path = os.path.join(root, afile)
                    if '_attachments' in path:
                        if CAN_MINIFY_JS:
                            min = options.minify
                        else:
                            min = False

                        tmp_root = os.path.join(root, afile)

                        tmp_path = list(path)
                        tmp_path.remove('_attachments')
                        tmp_path.append(afile)
                        tmp_path = os.path.join(*tmp_path)

                        attach = self._attach(tmp_path, tmp_root, min)
                        attachments.update(attach)
                    else:
                        if len(path) > 0 and path[0] in ['views', 'lists',
                                'shows', 'filters', 'indexes']:
                            f = open(afile_path)
                            d[afile.strip('.js')] = f.read().strip()
                            f.close()
                        else:
                            f = open(afile_path)
                            d[afile] = f.read().strip()
                            f.close()
                if d.keys():
                    app = recurse_update(app, reduce(nest, reversed(path), d))

        if attachments:
            app['_attachments'] = attachments
        return app

    def _minify(self, file):
        data = None
        try:
            f = open(file)
            mini = jsmin(f.read())
            data = base64.encodestring(mini)
            f.close()
        except:
            msg = "Could not minify %s, uploading expanded version"
            self.logger.debug(msg % file)
            data = base64.encodestring(open(file).read())
        return data

    def _process_url(self, url):
        """ Extract auth credentials from url, if present """
        parts = urlparse(url)
        if not parts.username and not parts.password:
            return url, None
        if parts.port:
            netloc = '%s:%s' % (parts.hostname, parts.port)
        else:
            netloc = parts.hostname
        url_tuple = (
                    parts.scheme,
                    netloc,
                    parts.path,
                    parts.params,
                    parts.query,
                    parts.fragment
                    )
        url = urlunparse(url_tuple)
        if parts.username and parts.password:
            auth_tuple = (parts.username, parts.password)
            auth = base64.encodestring('%s:%s' % auth_tuple).strip()
            return url, "%s" % auth
        else:
            auth_tuple = (parts.username, getpass.getpass())
            auth = base64.encodestring('%s:%s' % auth_tuple).strip()
            return url, "%s" % auth

    # cribbed from couchapp.py
    def read(self, fname, utf8=True, force_read=False):
        """ read file content"""
        if utf8:
            try:
                with codecs.open(fname, 'rb', "utf-8") as f:
                    return f.read()
            except UnicodeError:
                if force_read:
                    return read(fname, utf8=False)
                raise
        else:
            with open(fname, 'rb') as f:
                return f.read()

    # cribbed from couchapp.py
    def read_json(self, fname, use_environment=False, raise_on_error=False):
        """ read a json file and deserialize

        :attr filename: string
        :attr use_environment: boolean, default is False. If
        True, replace environment variable by their value in file
        content

        :return: dict or list
        """
        try:
            data = self.read(fname, force_read=True)
        except IOError, e:
            if e[0] == 2:
                return {}
            raise

        if use_environment:
            data = string.Template(data).substitute(os.environ)

        try:
            data = json.loads(data)
        except ValueError:
            logger.error("Json is invalid, can't load %s" % fname)
            if not raise_on_error:
                return {}
            raise
        return data

    # cribbed from couchapp.py
    def dir_to_fields(self, current_dir='', depth=0, manifest=[]):
        """ process a directory and get all members """

        fields = {}
        if not current_dir:
            current_dir = self.docdir
        for name in os.listdir(current_dir):
            current_path = os.path.join(current_dir, name)
            rel_path = _replace_backslash(os.path.relpath(current_path,
                                                       self.docdir))
            if name.startswith("."):
                continue
            # TODO: bring in .couchappignore support
            #elif self.check_ignore(name):
            #    continue
            elif depth == 0 and name.startswith('_'):
                # files starting with "_" are always "special"
                continue
            elif name == '_attachments':
                fields['_attachments'] = {}
                for root, dirs, files in os.walk(current_path):
                    for f in filter(self._allowed_file, files):
                        att_name = _replace_backslash(os.path.relpath(root, current_path))
                        if att_name == '.':
                            att_name = f
                        else:
                            att_name += '/' + f
                        fields['_attachments'][att_name] = self._attach(f, os.path.join(root, f))[f]
                continue
            elif depth == 0 and (name == 'couchapp' or
                                 name == 'couchapp.json'):
                # we are in app_meta
                if name == "couchapp":
                    manifest.append('%s/' % rel_path)
                    content = self.dir_to_fields(current_path,
                                                 depth=depth+1,
                                                 manifest=manifest)
                else:
                    manifest.append(rel_path)
                    content = self.read_json(current_path)
                    if not isinstance(content, dict):
                        content = {"meta": content}
                if 'signatures' in content:
                    del content['signatures']

                if 'manifest' in content:
                    del content['manifest']

                if 'objects' in content:
                    del content['objects']

                if 'length' in content:
                    del content['length']

                if 'couchapp' in fields:
                    fields['couchapp'].update(content)
                else:
                    fields['couchapp'] = content
            elif os.path.isdir(current_path):
                manifest.append('%s/' % rel_path)
                fields[name] = self.dir_to_fields(current_path, depth=depth+1,
                                                  manifest=manifest)
            else:
                self.logger.debug("push %s" % rel_path)

                content = ''
                if name.endswith('.json'):
                    try:
                        content = self.read_json(current_path)
                    except ValueError:
                        self.logger.error("Json invalid in %s" % current_path)
                else:
                    try:
                        content = self.read(current_path).strip()
                    except UnicodeDecodeError:
                        self.logger.warning("%s isn't encoded in utf8" %
                                       current_path)
                        content = self.read(current_path, utf8=False)
                        try:
                            content.encode('utf-8')
                        except UnicodeError:
                            self.logger.warning("plan B didn't work, %s is a binary"
                                           % current_path)
                            self.logger.warning("Move %s into _attachments" % current_path)
                            #self.logger.warning("use plan C: encode to base64")
                            #content = "base64-encoded;%s" % \
                            #    base64.b64encode(content)

                # remove extension
                name, ext = os.path.splitext(name)
                if name in fields:
                    self.logger.warning("%(name)s is already in properties. " +
                                   "Can't add (%(fqn)s)" % {"name": name,
                                                            "fqn": rel_path})
                else:
                    manifest.append(rel_path)
                    fields[name] = content
        return fields

    def run_command(self, args, options):
        """
        Build a python dictionary of the application, jsonise it and push it to
        CouchDB
        """
        self.logger.debug("Running Push Command for application in %s" %
                options.root)

        docs = os.path.join(options.root, '_docs')
        designs = os.path.join(options.root, '_design')
        apps_to_push = []
        attachments_to_push = []

        saved_servers = {}
        servers_to_use = {}
        if os.path.exists('servers.json'):
            saved_servers = json.load(open('servers.json'))

        for server in options.servers:
            if server in saved_servers.keys():
                servers_to_use[server] = saved_servers[server]
            else:
                url, auth = self._process_url(server)
                servers_to_use[server] = {"url": url}
                if auth:
                    servers_to_use[server]["auth"] = auth

        if len(servers_to_use.keys()) > 0:
            if options.only_docs == False:
                if os.path.exists(designs):
                    list_of_designs = os.listdir(designs)

                    if len(options.design) > 1:
                        list_of_designs = [options.design[1]]
                    for design in filter(self._allowed_file, list_of_designs):
                        name = os.path.join('_design', design)
                        root = os.path.join(designs, design)
                        app = self._walk_design(name, root, options)
                        apps_to_push.append(app)

            self._push_docs(apps_to_push, options.database, servers_to_use)

            if os.path.exists(docs):
                self.docdir = docs
                docs_to_push = self.dir_to_fields(docs)
                for key in docs_to_push:
                    if '_id' not in docs_to_push[key]:
                        docs_to_push[key]['_id'] = key
                self._push_docs(docs_to_push.values(), options.database,
                        servers_to_use)
        else:
            self.logger.warning('No servers specified - add -s server_url')


class Fetch(Command):
    """
    Copy a remote CouchApp into the working directory.
    """
    name = 'fetch'

    def _add_options(self):
        group = OptionGroup(self.parser, "Fetch options", "")
        group.add_option("-g", "--getdocs",
                dest="getdocs", action="store_true", default=False,
                help="Fetch documents as well as the design docs - potentially large response")
        self.parser.add_option_group(group)
    def run_command(self, args, options):
        """
        """
        url = '%s/_all_docs?include_docs=true' % args[0]
        if not options.getdocs:
            url = '%s&startkey="_design%%2F"&endkey="_design0"' % url
        d = json.load(urllib.urlopen(url))
        app = [d['doc'] for d in d['rows']]

        if not os.path.exists(options.root):
            os.mkdir(options.root)
        if options.getdocs and not os.path.exists('%s/_docs' % options.root):
            os.mkdir('%s/_docs' % options.root)
        if not os.path.exists('%s/_design' % options.root):
            os.mkdir('%s/_design' % options.root)
        for doc in app:
            # TODO: have _rev removal be optional
            # TODO: correct on disk layout of vendors
            # FIXME: ignores design docs without _attachments
            del doc['_rev']
            id = str(doc['_id'])
            attachments = doc.get('_attachments', {})
            if attachments:
                del doc['_attachments']
            if id.startswith('_design'):
                path_elems = os.path.join(options.root, id).split('/')
                path_elems.append('_attachments')
                base_att_dir = os.path.join(*path_elems)
                # This could (and should) be loads nicer
                views = os.path.join(options.root, id, 'views')
                if not os.path.exists(os.path.join(options.root, id)):
                    os.mkdir(os.path.join(options.root, id))
                if 'views' in doc.keys() and not os.path.exists(views):
                    os.mkdir(views)
                for view, content in doc['views'].items():
                    p = os.path.join(views, view)
                    if not os.path.exists(p):
                        os.mkdir(p)
                    for fn, fnc in content.items():
                        f = open(os.path.join(p, '%s.js' % fn), 'w')
                        f.write(fnc)
                        f.close()

            else:
                f = open(os.path.join('%s/_docs' % options.root, '%s.json' % id), 'w')
                json.dump(doc, f)
                f.close()
                base_att_dir = os.path.join('%s/_docs' % options.root, id)
            for att in attachments.keys():
                att_dir = os.path.join(base_att_dir, *att.split('/')[:-1])
                if not os.path.exists(att_dir):
                    os.makedirs(att_dir)

                a_file = str(os.path.join(att_dir, att.split('/')[-1]))
                urllib.urlretrieve('%s/%s/%s' % (url, id, att), a_file)


class InstallVendor(Command):
    """
    Command to install a vendor from a remote source.
    """
    name = "vendor"
    no_required_args = 1

    def _add_options(self):
        group = OptionGroup(self.parser, "Vendor options", "")

        group.add_option("--ext_version",
          default='latest', dest="ext_version",
          help="Install a specific version of the external, default is latest")

        self.parser.add_option_group(group)

    def run_command(self, args, options):
        """
        """
        vendor = FetchVendors()
        vendor(args, options)


class Generator(Command):
    """
    A generator knows how to create files and where to create them.
    """
    # _template is a dict of filename:it's content
    _template = {}
    # the type of thing the generator generates
    name = "generator_interface"
    path_elem = None

    def __init__(self):
        self.usage = "usage: %prog " + self.name + " [options] [args]"
        Command.__init__(self)

    def run_command(self, args, options):
        """
        Run the generator
        """
        #self.process_args(args, options)
        path = None
        if self.name == "view" and len(args):
            path = self._create_path(options.root, options.design, args[0])
        else:
            path = self._create_path(options.root, options.design)
        self._push_template(path, args, options)

    def _create_path(self, root, design=[], name=None, misc=None):
        """
        Create the path the generator needs
        """
        if os.path.exists(root):
            path_elems = [root]
            if len(design) > 1:
                path_elems.extend(design)

            if name:
                if not self.path_elem:
                    self.path_elem = self.name
            path_elems.extend([self.path_elem, name])

            if misc:
                path_elems.extend(misc)

            path_elems = [item for item in path_elems if item != None]
            path = os.path.join(*tuple(path_elems))
            self.logger.debug('Creating: %s' % path)
            if not os.path.exists(path):
                os.makedirs(path)
            return path
        else:
            raise OSError('Application directory (%s) does not exist' % root)

    def _write_file(self, path, content):
        """
        Write content to a file.
        """
        f = open(path, 'w')
        f.write(content)
        f.write('\n')
        f.close()

    def _write_json(self, path, obj):
        """
        Write an object to json
        """
        f = open(path, 'w')
        json.dump(obj, f)
        f.close()

    def _push_template(self, path, args, options):
        """
        Create files following _templates
        """
        path = os.path.join(path, '%s.js' % args[0].replace('.js', ''))
        self._write_file(path, self._template[self.name])


class View(Generator):
    """
    Create the map.js and reduce.js files for a view. Can use built in erlang
    reducers (faster) for the reduce.js (see options above).
    """
    name = "view"
    path_elem = "views"
    _template = {
        'map.js': '''function(doc){
  emit(null, 1)
}''',
        'reduce.js': '''function(key, values, rereduce){

}''',
    }

    def _add_options(self):
        """
        Allow for using a built in reduce.
        """
        self.parser.add_option("--builtin-reduce",
                    dest="built_in", default=False,
                    choices=['sum', 'count', 'stats'],
                    help="Use a built in reduce (one of sum, count, stats)")

        for reducer in ['sum', 'count', 'stats']:
            help_msg = "Use the %s built in reduce, shorthand" % reducer
            help_msg += " for --builtin-reduce=%s" % reducer
            self.parser.add_option("--%s" % reducer,
                    dest="built_in", default=False,
                    action="store_const", const=reducer,
                    help=help_msg
                    )

    def _push_template(self, path, args, options):
        """
        Create files following _templates, built_in should be either unset
        (False) or be the name of a built in reduce function.
        """
        reduce_file = os.path.join(path, 'reduce.js')
        map_file = os.path.join(path, 'map.js')
        self._write_file(map_file, self._template['map.js'])
        if options.built_in:
            self._write_file(reduce_file, '_%s' % options.built_in)
        else:
            self._write_file(reduce_file, self._template['reduce.js'])


class ListGen(Generator):
    name = "list"
    path_elem = "lists"
    _template = {'list': '''function(head, req) {
  var row;
  start({
    "headers": {
      "Content-Type": "text/html"
     }
  });
  while(row = getRow()) {
    send(row.value);
  }
}'''}


class Search(Generator):
    name = "search"
    path_elem = "indexes"
    _template = {'search': '''function(doc) {

}'''}


class Show(Generator):
    name = "show"
    path_elem = "shows"
    _template = {'show': '''function(doc, req) {

}'''}


class Filter(Generator):
    name = "filter"
    path_elem = "filters"
    _template = {'filter': '''function(doc, req) {
  return true;
}'''}


class Update(Generator):
    name = "update"
    path_elem = "updates"
    _template = {'update': """function(doc, req) {

}"""}


class Validation(Generator):
    name = "validation"
    path_elem = "validate_doc_update"
    _template = {'validation': """function(newDoc, oldDoc, userCtx) {
  throw({forbidden : 'no way'});
}"""}


class GitHook(Generator):
    """
    Write a post commit git hook such that situp.py push is called after git
    commit.
    """
    name = "githook"
    path_elem = ".git/hooks"
    _template = {'githook': './situp.py push'}

    def _push_template(self, path, args, options):
        file = os.path.join(path, 'post-commit')
        self._write_file(file, self._template[self.name])
        os.chmod(file, stat.S_IXUSR | stat.S_IWUSR | stat.S_IRUSR)
        self.logger.info("Created post-commit hook in %s" % file)


class Document(Generator):
    """
    Create an empty json document (containing just an _id) in the _docs folder
    of the application root.
    """
    name = 'document'
    path_elem = '_docs'
    _template = {'document': {}}

    def _add_options(self):
        self.parser.add_option("--name",
                    dest="name",
                    help="Name the document")

    def _push_template(self, path, args, options):
        path = self._create_path(options.root)
        file_name = str(uuid.uuid1())
        doc = self._template['document']
        doc['_id'] = file_name
        if options.ensure_value('name', False):
            doc['_id'] = options.name
            file_name = options.name
        doc_file = os.path.join(path, file_name)

        self._write_json(doc_file, doc)


class Html(Document):
    """
    Create an empty html document in the _attachments folder of the specified
    design document.
    TODO: include script tags for all vendors in generated html.
    """
    name = 'html'
    path_elem = '_attachments'
    _template = {
        'document': '''<html>
    <head>
        <title>REPLACE</title>
    </head>
    <body>
        <h1>REPLACE</h1>
    </body>
</html>'''
    }
    required_opts = ['name']

    def _add_options(self):
        self.parser.add_option("--name",
                    dest="name", help="Name the document")

    def _push_template(self, path, args, options):
        file_name = '%s.html' % options.name.split('.htm')[0]
        title = options.name.split('.htm')[0].title()
        doc = self._template['document'].replace('REPLACE', title)
        doc_file = os.path.join(path, file_name)

        self._write_file(doc_file, doc)


def fetch_archive(url, path, filter_list=[]):
    """
    Fetch a remote tar/zip archive and extract it, applying a filter if one is
    provided.
    """
    (filename, response) = urllib.urlretrieve(url)
    subfolder = ""
    if tarfile.is_tarfile(filename):
        tgz = tarfile.open(filename)
        to_extract = tgz.getmembers()
        subfolder = to_extract[0].name
        if filter_list:
            #lambda f: f.name in filter_list
            def filter_this(f):
                return filter(lambda g: f.name.endswith(g), filter_list)
            for member in filter(filter_this, to_extract):
                tgz.extract(member, path)
        else:
            tgz.extractall(path)
        tgz.close()
    elif zipfile.is_zipfile(filename):
        myzip = zipfile.ZipFile(filename)
        to_extract = myzip.infolist()
        subfolder = to_extract[0].filename
        if filter_list:
            def filter_this(f):
                return filter(lambda g: f.filename.endswith(g), filter_list)
            for member in filter(filter_this, to_extract):
                myzip.extract(member, path)
        else:
            myzip.extractall(os.path.join(path, '_attachments'))
        myzip.close()
    else:
        print 'ERROR: %s is not a readable archive' % url
        sys.exit(-1)
    # TODO: use a --force option
    try:
        shutil.rmtree(os.path.join(path, '_attachments'))
    except:
        pass
    dest = os.path.join(path, '_attachments/')
    os.mkdir(dest)
    for sfile in os.listdir(os.path.join(path, subfolder)):
        source = os.path.join(path, subfolder, sfile)
        shutil.move(source, dest)

    shutil.rmtree(os.path.join(path, subfolder))
    os.remove(filename)


Package = namedtuple('Package', ['url', 'filter'])


class FetchVendors(Generator):
    """
    Vendors are generators that download external code into the right place.
    The code is held in kanso packages, and situp assumes that these have been
    correctly built.
    """

    name = "vendor"

    def __call__(self, args, options):
        """
        Override call here - need to pass in the args/options instead of get
        them from optparse.
        """
        self._configure_logger(options)

        self.logger.debug('called')
        self.logger.debug(args)
        self.logger.debug(options)

        self.run_command(args, options)

    def install_external(self, external, options, vendor_path=None):
        """ Install external """
        if not vendor_path:
            vendor_path = self._create_path(options.root, options.design)
        path = self._create_path(vendor_path, [], external)

        self.logger.debug('Installing %s into %s' % (external, path))
        # TODO: catch not founds etc
        url = "http://kan.so/repository/%s" % external
        (filename, response) = urllib.urlretrieve(url)
        package = json.load(open(filename))
        version = options.ext_version

        if version == 'latest':
            version = package['tags']['latest']
        if 'dependencies' in package['versions'][version] and\
                len(package['versions'][version]['dependencies']) > 0:
            self.logger.info('Fetching dependencies for %s' % external)
            for dep in package['versions'][version]['dependencies'].keys():
                if dep not in os.listdir(vendor_path):
                    opt = options
                    # TODO: work out the right version for a dependency
                    opt.ext_version = 'latest'
                    self.install_external(dep, options, vendor_path)
        archive = "%s-%s.tar.gz" % (external, version)
        fetch_archive(url + '/' + archive, path)
        self.logger.info("Installed %s to %s" % (external, path))

    def run_command(self, args, options):
        """
        Vendors behave differently to other generators
        """
        self.logger.warning("Fetching externals, may take a while")
        for external in args:
            self.install_external(external, options)


if __name__ == "__main__":
    cli = CommandDispatch()
    for command in [AddServer, Push, Fetch, InstallVendor, View, ListGen, Show,
            Search, Document, Html, GitHook, Filter, Update, Validation]:
        cli.register_command(command())

    if len(sys.argv) > 1 and sys.argv[1] in cli.commands.keys():
        cli(sys.argv[1])
    else:
        cli()
