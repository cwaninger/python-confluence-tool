from urlparse import urlparse
from .page import Page
import re, json
import requests
from .storage_editor import StorageEditor
from .page_properties import PagePropertiesEditor

import logging
logger = logging.getLogger('confluence.api')

def is_string(s):
    # python 2.7
    return isinstance(s, basestring)

class ConfluenceError(StandardError):
    pass

class ConfluenceAPI:
    def __init__(self, config):
        self.config = config
        self.hostname = urlparse(config['baseurl']).hostname

    def set_args(self, args):
        self.args = args

    def get_args(self):
        return self.args

    def _getauth(self):
        '''return tuple (user, password) for accessing confluence'''
        import netrc

        if self.config.get('username'):

            if not self.config['password']:
                import keyring
                baseurl = self.config['baseurl']
                password = keyring.get_password(baseurl, self.config['username'])
                if password is None:
                    import getpass
                    password = getpass

            else:
                password = self.config.get('password')

            if password is not None:
                return (self.config['username'], password)

        logger.info('hostname: %s', self.hostname)
        (user, account, password) = netrc.netrc().authenticators(self.hostname)
        return (user, password)

    def __getattr__(self, name):
        if name == 'session':
            self.session = requests.Session()
            self.session.auth = self._getauth()
            return self.session

        raise AttributeError(name)

    def request(self, method, endpoint, params=None, **kwargs):
        url = self.config['baseurl'] + endpoint
        if params is None:
            params = {}

        headers = { 'X-Atlassian-Token': 'no-check' }
        if isinstance(params, dict):
            params.update(kwargs)

        try:
            if method == 'GET':
                response = self.session.request(method, url, params=params, headers=headers)
            else:
                headers.update({'Content-Type': 'application/json', 'Accept':'application/json'})
                response = self.session.request(method, url, data=json.dumps(params), headers=headers)

        except StandardError as e:
            logger.info("error in request %s %s with params %s", method, endpoint, params)
            raise

        if response.status_code >= 400:
            self.session.close()
            del self.session

            error = ''
            logger.info("error: %s %s, %s: %s", method, url, params, response.text)

            raise ConfluenceError(response.text)

        if response.text:
            return response.json()

        return {}

    def get(self, endpoint, params=None, **kwargs):
        return self.request('GET', endpoint, params, **kwargs)

    def put(self, endpoint, params=None, **kwargs):
        return self.request('PUT', endpoint, params, **kwargs)

    def post(self, endpoint, params=None, **kwargs):
        return self.request('POST', endpoint, params, **kwargs)

    def delete(self, endpoint, params=None, **kwargs):
        return self.request('DELETE', endpoint, params, **kwargs)

    def createSpace(self, key, name, description=''):
        return self.post( '/rest/api/space',
            key=key, name=name, type='global', description={
                'plain': {
                    'value': description,
                    'representation': 'plain'
                }
            })

    def copySpace(self, source_key, key, name, description=''):
        source_space = self.getSpace(source_key)

        # create new space
        space = self.createSpace(key, name, description)
        target_space = self.getSpace(key)
        #log.info("Created new ps")

        self.copyPage(source_space['_expandable']['homepage'], target_space['_expandable']['homepage'])

        # self.updatePage(target_page['id'], version=target_page['version']['number'], title=target_page['title'], storage=source_page['body']['storage']['value'])
        #
        # subpages = self.getChildren(source_page['id'], type='page')
        # # now create page tree from source page
        # for subpage in subpages:
        #     sp = self.getPage(subpage['id'], expand='body.storage')
        #     self.createPage(target_space['key'], subpage['title'], storage = sp['body']['storage']['value'], parent=target_page['id'])

    def getUser(self, username, expand=''):
        """get user information"""
        return self.get("/rest/api/user", username=username, expand=expand)

    def copyPage(self, source, target=None, recursive=True, parent=None, space=None, delete=False):
        '''copy source page as child of target and descend all children

        :param source:
           content url or a pageid
        :param recursive:
           (default True) copy children
        :param parent:
           parent for current target
        :param target:
           where to copy a page
        :param space:
           where to copy a page
        :param delete:
           delete children not present in source
        '''

        source_page = self.getPage(source, expand='body.storage,space')
        target_page = self.getPage(target, expand='version,space')

        if target_page is None:
            target_page = self.createPage(
                space   = space,
                title   = target,
                storage = source_page['body']['storage']['value'],
                parent  = parent
            )
            logger.info("Create Page: %s, %s", space, target)
        else:
            self.updatePage( target_page['id'],
                version = target_page['version']['number'],
                title   = target_page['title'],
                storage = source_page['body']['storage']['value']
            )
            logger.info("Update Page: %s, %s", space, target_page['title'])

        if recursive:
            source_subpages = sorted([ (p['title'], p) for p in self.getChildren(source_page['id'], type='page') ])
            target_subpages = sorted([ (p['title'], p) for p in self.getChildren(target_page['id'], type='page') ])

            # TODO: assert that there are no duplicate page titles

            source_subpage = dict(source_subpages)
            target_subpage = dict(target_subpages)

            assert len(source_subpage.keys()) == len(source_subpages)
            assert len(target_subpage.keys()) == len(target_subpages)

            for title, page in source_subpage.items():
                if title in target_subpage:
                    subpage = target_subpage[title]['id']
                else:
                    subpage = title

                self.copyPage(page['id'], target=subpage, recursive=recursive,
                    parent=target_page['id'], space=space)

            if delete:
                for title, page in target_subpage.items():
                   if title not in source_subpage:
                       self.deletePage(page['id'])

    def getSpace(self, space_key, expand=''):
        return self.get( '/rest/api/space/%s' % space_key, expand=expand)

    def listSpaces(self, expand=''):
        return self.get('/rest/api/space', expand=expand, limit=1000)['results']

    def getPage(self, page_id, expand=''):
        if isinstance(expand, (list, set)):
            expand=",".join(expand)

        if not page_id.startswith('/rest'):
            page_id = '/rest/api/content/%s' % page_id

        return Page(self, self.get( page_id, expand=expand), expand=expand)

    def getPages(self, cql, expand='', filter=None):
        if not expand:
            expand = []

        if filter is not None:
            for page in self.getPagesWithProperties(cql, page_prop_filter=filter, expand=expand):
                yield page

        else:
            for page in self.iterate(cql=cql, expand=expand):
                yield Page(self, page, expand)

    def getSpaceHomePage(self, space_key):
        logger.info("space_key: %s", space_key)
        homepage = self.getSpace(space_key, expand='homepage')['homepage']['id']
        return homepage

    def convertWikiToStorage(self, content):
        """convert wiki to storage representation

        Returns storage representation.
        """
        return self.post_json('/rest/api/contentbody/convert/storage',
            value=content, representation='wiki')['value']

    def addLabels(self, page_id, labels):
        if not isinstance(labels, list):
            labels = [ labels ]
        if not isinstance(labels[0], dict):
            labels = [ dict(prefix="global", name=lbl) for lbl in labels ]

        logger.info("labels: %s", labels)
        return self.post('/rest/api/content/%s/label' % page_id, labels)


    def updatePage(self, id, title, body=None, version=None, type='page', storage=None, wiki=None):
        if not isinstance(version, dict):
            version = {'number': int(version)}

        if storage is not None:
            body = {
                'storage': {
                    'value': storage,
                    'representation': 'storage'
                }
            }
        if wiki is not None:
            body = {
                'storage': {
                    'value': wiki,
                    'representation': 'wiki'
                }
            }

        return self.put('/rest/api/content/%s' % id,
            version = version,
            type    = type,
            title   = title,
            body    = body
        )

    def editPage(self, cql, editor, filter=None):
        """
        Editor works with mustache templates and jQuery assingments.

        editor: must be either a list (of actions) or must be a dictionary with following items:

            * `templates` - a dictionary of templates, which can be used in
              edit actions
            * `partials` - a dictionary of partials, which can be used in
              templates
            * `actions` - an array of editor actions, where each item is a
              dictionary with following items:

              * `select` - (required) a jQuery selector
              * `action` - a jQuery method how to apply content to selection
                (default: html)
              * `content` - content to be applied. If not present, `template`
                and `data` must be present
              * `type` - type of content.  May be either `storage` or `wiki`.
                Default is `storage`
              * `templates` - a local set of templates to be overridden
              * `template` - a template to which data is applied to, for
                generating content.
              * `data` - data to be applied to template
        """

        editor = StorageEditor(editor)

        for page in self.getPages(cql, filter=filter):
            yield page, editor.edit(page)


    def getPageVersion(self, page_id):
        data = self.get('/rest/api/content/%s' % page_id)
        return data['version']['number']

    def findPages(self, pageSpec='', expand='', limit='', start='', cql=''):
        '''pageSpec may be page_id, "space:path" or CQL (see https://developer.atlassian.com/confdev/confluence-server-rest-api/advanced-searching-using-cql)'''

        if pageSpec:
            cql = self.resolveCQL(pageSpec)
        if isinstance(expand, (list, set)):
            expand = ','.join(list(expand))

        return self.get('/rest/api/content/search', cql = cql, expand=expand, limit=limit, start=start)

    def iterate(self, *args, **kwargs):
        if 'start' not in kwargs:
            kwargs['start'] = 0
        if 'limit' not in kwargs:
            kwargs['limit'] = -1

        start = kwargs['start']
        limit = 25 # confluence max
        done = False
        maxResults = kwargs['limit']
        if maxResults < 0:
            maxResults = 10000000000

        while True:
            kwargs['start'] = start
            kwargs['limit'] = limit
            result = self.findPages(*args, **kwargs)

            for page in result['results']:
                logger.info("page_id: %s", page['id'])
                yield page
                maxResults -= 1
                if maxResults <= 0:
                    break

            if maxResults <= 0:
                break

            start += limit
            if result['size'] < result['limit']:
                break

            logger.info("next round: start=%s, limit=%s, size=%s, result_limit=%s", start, limit, result['size'], result['limit'])

    SPACE_PAGE_REF = re.compile(r'^([A-Z]*):(.*)$')
    PAGE_REF = re.compile(r'^:(.*)$')
    PAGE_ID = re.compile(r'^(\d+)$')
    PAGE_URI = re.compile(r'api/content/(\d+)$')

    def resolveCQL(self, ref):
        """resolve some string to CQL query

        :param ref:
            resolve this reverence to a valid CQL

            * ``SPACE:page title`` -> ``space = SPACE and title = "page title"``
            * ``:page title`` ->  ``title = "page title"``
            * ``12345`` -> ``ID = 12345``
            * ends with ``api/content/12345`` -> ``ID = 12345``
            * else assume ``ref`` is already CQL

        :return:
            CQL
        """
        def match(RE):
            m = RE.search(ref)
            if m:
                self.mob = m.groups()
                return True
            else:
                return False

        if not is_string(ref):
            ref = str(ref)

        if match(self.SPACE_PAGE_REF):
            return "space = {} AND title  = \"{}\"".format(*self.mob)

        if match(self.PAGE_REF):
            return "title  = \"{}\"".format(*self.mob)

        if match(self.PAGE_ID):
            return "ID  = {}".format(*self.mob)

        if match(self.PAGE_URI):
            return "ID  = {}".format(*self.mob)

        return ref


    def setPageProperties(self, document):
        pages = document.pop('pages', None)

        if pages is not None:
            for page in pages:
                _doc = document.copy()
                _doc.update(page)
                for p in self.setPageProperties(_doc):
                    yield p

        cql = None
        if 'page' in document:
            cql = self.resolveCQL(document['page'])
        elif 'cql' in document:
            cql = document['cql']

        if cql is not None:
            editor = PagePropertiesEditor(confluence=self, **document)

            found = False
            for page in self.getPagesWithProperties(cql, expand=['body.storage', 'version']):
                new_content = editor.edit(page)
                found = True
                yield dict(
                    page    = page,
                    content = new_content,
                    result  = self.updatePage(
                        id = page['id'],
                        version = int(page['version']['number'])+1,
                        title   = page['title'],
                        storage = new_content
                    ))

            if not found:
                new_content = editor.edit()
                (space, title) = document['page'].split(':', 1)
                yield dict(
                    page    = dict(
                        spacekey = space,
                        title    = title,
                        ),
                    content = new_content,
                    result  = self.createPage(
                        space = space,
                        title = title,
                        storage = new_content,
                    ))



    PAGE_PROP_FILTER = re.compile(r'^(.*?)([!=])=(.*)')
    def getPagesWithProperties(self, cql, page_prop_filter=None, expand=[], **options):
        page_prop_filters = []

        cql = self.resolveCQL(cql)

        if page_prop_filter is not None:
            if not isinstance(page_prop_filter, list):
                page_prop_filter = [ page_prop_filter ]

            for item in page_prop_filter:
                if is_string(item):
                    m = self.PAGE_PROP_FILTER.search(item)
                    if m:
                        (name, cmp, value) = m.groups()
                        page_prop_filters.append(dict(name=name, cmp=cmp, value=value))
                else:
                    page_prop_filters.append(item)

        def page_prop_filterer(page):
            if not len(page_prop_filters):
                return True

            result = True
            for f in page_prop_filters:
                value = page.getPageProperty(f['name'])

                if cmp == '=':
                    if isinstance(value, list):
                        result = result and f['value'] in value
                    else:
                        result = result and value == f['value']
                else:
                    if isinstance(value, list):
                        result = result and f['value'] not in value
                    else:
                        result = result and value != f['value']

            return result

        for page in filter(page_prop_filterer, self.getPages(cql, expand=expand + ['body.view'])):
            yield page

    def extractPage(self, pageSpec):
        results = self.findPages(pageSpec, expand='space')

        if results is None:
            return None

        assert results['size'] == 1, "Ambigious search: %s" % pageSpec

        page = results['results'][0]
        return page['space']['key'], page['title'], page['id']

    def createPage(self, space, title, storage, parent=None):
        data = dict(
            title = title,
            type  = 'page',
            space = {'key': space},
            body  = {'storage': {'value': storage, 'representation': 'storage'}}
        )

        if parent is not None:
            data['ancestors'] = [{'id': parent}]

        return self.post('/rest/api/content', **data)


    def getChildren(self, page_id, type=None, expand=''):
        if not page_id.startswith('/rest'):
            page_id = '/rest/api/content/%s' % page_id
        if type is None:
            url = page_id +"/child"
        else:
            url = page_id + "/child/"+type
        return self.get(url, expand=expand)['results']


if 0:
  class ConfluenceAPI:
    def __init__(self, url, username=None, password=None, version=None):
        '''Create ConfluenceAPI object

        :param url:
            Base URL of Confluence Server.
        :param username:
            Optional Username for connecting
        :param password:
            Optional Password for connecting
        :param version:
            Optional Version of the API (not used for now)

        If you do not pass username (and password), it is checked, if in URL,
        else it is tried to read it from netrc(5) facility.
        '''

        parsed_url = urlparse(url)
        if username is None:
            if parsed_url.username:
                self.username = parsed_url.username
                self.password = parsed_url.password
            else:
                auth_data = netrc.netrc().authenticators(parsed_url.hostname)
                self.username = auth_data[0]
                self.password = auth_data[2]

        urldata = dict(scheme=parsed_url.scheme, netloc=parsed_url.netloc)
        self.url = "{scheme}://{netloc}".format(urldata)

    def __getattr__(self, name):
        '''autofill some attributes

        :param name:
            - `session` get a HTTP session variable
        '''

        if name == 'session':
            self.session = requests.Session()
            self.session.auth = (self.username, self.password)
            return self.session

        raise AttributeError(name)

    def request(self, method, path, params={}, files={}, data=None, json=None):
        '''do an HTTP request

        :param method:
            method to do with request
        :param path:
            path to be appended to base URL for request
        :param params:
            parameters to be sent with request (depending on method)

        '''

        # compose URL
        url = self.url + path

        # remove params with None value
        for k in params.keys():
            if params[k] is None:
                del params[k]

        # setup headers
        headers = None
        if len(files):
            headers = {"X-Atlassian-Token" : "no-check"}

        # if JSON present, send JSON body
        if json:
            if headers is None:
                headers = {}
            headers['Content-Type'] = "application/json"
            data = serialize(json)
            logger.info("json Data: %s", data)

        try:
            response = self.session.request(method, url, params=params,
                files=files, headers=headers, data=data)
        except Exception as e:
            logger.info("error in request %s, %s", url, params, exc_info=1)
            raise

        if response.status_code >= 400:
            self.session.close()
            error = ""
            try:
                text = json.loads(response.text)
                if u'errorMessage' in text:
                    error = text[u'errorMessage']
                elif u'errorMessages' in text:
                    error = "\n".join([error] + text[u'errorMessages'])
                elif 'message' in text:
                    error = text['message']

            except:
                error = response.text

            raise RuntimeError("Confluence API returned %d: %s\nRequested URL: %s\n%s" % (response.status_code, response.reason, url, error))

        if response.text:
            return response.json()
        else:
            return response

    def get(self, resturl, files = {}, **params):
        return self.request('get', resturl, params, files)

    def post(self, resturl, files={}, **params):
        return self.request('post', resturl, params, files)

    def post_json(self, resturl, files={}, **params):
        return self.request('post', resturl, files, json=params)

    def put(self, resturl, files={}, **params):
        return self.request('put', resturl, params, files)

    def put_json(self, resturl, files={}, **params):
        return self.request('put', resturl, files, json=params)

    def delete(self, resturl, files={}, **params):
        return self.request('delete', resturl, params, files)

    # Spaces
    # ------------------------------------------------------

    def createSpace(self, key, name, type='global', description=None, **kwargs):
        params = kwargs.copy()

        params.update(key=key, name=name, type=type)