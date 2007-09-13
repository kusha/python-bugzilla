#!/usr/bin/python
# bugzilla.py - a Python interface to bugzilla.redhat.com, using xmlrpclib.
#
# Copyright (C) 2007 Red Hat Inc.
# Author: Will Woods <wwoods@redhat.com>
# 
# This program is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation; either version 2 of the License, or (at your
# option) any later version.  See http://www.gnu.org/copyleft/gpl.html for
# the full text of the license.

import xmlrpclib, urllib2, cookielib
import os.path, base64, copy

version = '0.2'
user_agent = 'bugzilla.py/%s (Python-urllib2/%s)' % \
        (version,urllib2.__version__)

class Bugzilla(object):
    '''An object which represents the data and methods exported by a Bugzilla
    instance. Uses xmlrpclib to do its thing. You'll want to create one thusly:
    bz=Bugzilla(url='https://bugzilla.redhat.com/xmlrpc.cgi',user=u,password=p)

    If you so desire, you can use cookie headers for authentication instead.
    So you could do:
    cf=glob(os.path.expanduser('~/.mozilla/firefox/default.*/cookies.txt'))
    bz=Bugzilla(url=url,cookies=cf)
    and, assuming you have previously logged info bugzilla with firefox, your
    pre-existing auth cookie would be used, thus saving you the trouble of
    stuffing your username and password in the bugzilla call.
    On the other hand, this currently munges up the cookie so you'll have to
    log back in when you next use bugzilla in firefox. So this is not
    currently recommended.

    The methods which start with a single underscore are thin wrappers around
    xmlrpc calls; those should be safe for multicall usage.
    '''
    def __init__(self,**kwargs):
        # Settings the user might want to tweak
        self.user       = ''
        self.password   = ''
        self.url        = ''
        # Bugzilla object state info that users shouldn't mess with
        self._cookiejar  = None
        self._proxy      = None
        self._opener     = None
        self._querydata  = None
        self._querydefaults = None
        self._products   = None 
        self._components = dict()
        if 'cookies' in kwargs:
            self.__readcookiefile(kwargs['cookies'])
        if 'url' in kwargs:
            self.connect(kwargs['url'])
        if 'user' in kwargs:
            self.user = kwargs['user']
        if 'password' in kwargs:
            self.password = kwargs['password']

    #---- Methods for establishing bugzilla connection and logging in

    def __readcookiefile(self,cookiefile):
        '''Read the given (Mozilla-style) cookie file and fill in the cookiejar,
        allowing us to use the user's saved credentials to access bugzilla.'''
        cj = cookielib.MozillaCookieJar()
        cj.load(cookiefile)
        self._cookiejar = cj
        self._cookiejar.filename = cookiefile

    def connect(self,url):
        '''Connect to the bugzilla instance with the given url.'''
        # Set up the transport
        if url.startswith('https'):
            self._transport = SafeCookieTransport()
        else:
            self._transport = CookieTransport() 
        self._transport.user_agent = user_agent
        self._transport.cookiejar = self._cookiejar or cookielib.CookieJar()
        # Set up the proxy, using the transport
        self._proxy = xmlrpclib.ServerProxy(url,self._transport)
        # Set up the urllib2 opener (using the same cookiejar)
        handler = urllib2.HTTPCookieProcessor(self._cookiejar)
        self._opener = urllib2.build_opener(handler)
        self._opener.addheaders = [('User-agent',user_agent)]
        self.url = url

    # Note that the bugzilla methods will ignore an empty user/password if you
    # send authentication info as a cookie in the request headers. So it's
    # OK if we keep sending empty / bogus login info in other methods.
    def login(self,user,password):
        '''Attempt to log in using the given username and password. Subsequent
        method calls will use this username and password. Returns False if 
        login fails, otherwise returns a dict of user info.
        
        Note that it is not required to login before calling other methods;
        you may just set user and password and call whatever methods you like.
        '''
        self.user = user
        self.password = password
        try: 
            r = self._proxy.bugzilla.login(self.user,self.password)
        except xmlrpclib.Fault, f:
            r = False
        return r

    #---- Methods and properties with basic bugzilla info 

    def _multicall(self):
        '''This returns kind of a mash-up of the Bugzilla object and the 
        xmlrpclib.MultiCall object. Methods you call on this object will be added
        to the MultiCall queue, but they will return None. When you're ready, call
        the run() method and all the methods in the queue will be run and the
        results of each will be returned in a list. So, for example:

        mc = bz._multicall()
        mc._getbug(1)
        mc._getbug(1337)
        mc._query({'component':'glibc','product':'Fedora','version':'devel'})
        (bug1, bug1337, queryresult) = mc.run()
    
        Note that you should only use the raw xmlrpc calls (mostly the methods
        starting with an underscore). Normal getbug(), for example, tries to
        return a Bug object, but with the multicall object it'll end up empty
        and, therefore, useless.

        Further note that run() returns a list of raw xmlrpc results; you'll
        need to wrap the output in Bug objects yourself if you're doing that
        kind of thing. For example, Bugzilla.getbugs() could be implemented:

        mc = self._multicall()
        for id in idlist:
            mc._getbug(id)
        rawlist = mc.run()
        return [Bug(self,dict=b) for b in rawlist]
        '''
        mc = copy.copy(self)
        mc._proxy = xmlrpclib.MultiCall(self._proxy)
        def run(): return mc._proxy().results
        mc.run = run
        return mc

    def _getqueryinfo(self):
        return self._proxy.bugzilla.getQueryInfo(self.user,self.password)
    def getqueryinfo(self,force_refresh=False):
        '''Calls getQueryInfo, which returns a (quite large!) structure that
        contains all of the query data and query defaults for the bugzilla
        instance. Since this is a weighty call - takes a good 5-10sec on
        bugzilla.redhat.com - we load the info in this private method and the
        user instead plays with the querydata and querydefaults attributes of
        the bugzilla object.'''
        # Only fetch the data if we don't already have it, or are forced to
        if force_refresh or not (self._querydata and self._querydefaults):
            (self._querydata, self._querydefaults) = self._getqueryinfo()
        return (self._querydata, self._querydefaults)
    # Set querydata and querydefaults as properties so they auto-create
    # themselves when touched by a user. This bit was lifted from YumBase,
    # because skvidal is much smarter than I am.
    querydata = property(fget=lambda self: self.getqueryinfo()[0],
                         fdel=lambda self: setattr(self,"_querydata",None))
    querydefaults = property(fget=lambda self: self.getqueryinfo()[1],
                         fdel=lambda self: setattr(self,"_querydefaults",None))

    def _getproducts(self):
        return self._proxy.bugzilla.getProdInfo(self.user, self.password)
    def getproducts(self,force_refresh=False):
        '''Return a dict of product names and product descriptions.'''
        if force_refresh or not self._products:
            self._products = self._getproducts()
        return self._products
    # Bugzilla.products is a property - we cache the product list on the first
    # call and return it for each subsequent call.
    products = property(fget=lambda self: self.getproducts(),
                        fdel=lambda self: setattr(self,'_products',None))

    def _getcomponents(self,product):
            return self._proxy.bugzilla.getProdCompInfo(product, 
                                        self.user,self.password)
    def getcomponents(self,product,force_refresh=False):
        '''Return a dict of components for the given product.'''
        if force_refresh or product not in self._components:
            self._components[product] = self._getcomponents(product)
        return self._components[product]
    # TODO - add a .components property that acts like a dict?

    def _get_info(self,product=None):
        '''This is a convenience method that does getqueryinfo, getproducts,
        and (optionally) getcomponents in one big fat multicall. This is much
        faster than calling them all separately.
        
        If you're doing interactive stuff you should call this, with the
        appropriate product name, after connecting to Bugzilla. This will
        cache all the info for you and save you an ugly delay later on.'''
        mc = self._multicall()
        mc._getqueryinfo()
        mc._getproducts()
        if product:
            mc._getcomponents(product)
        r = mc.run()
        (self._querydata,self._querydefaults) = r[0]
        self._products = r[1]
        if product:
            self._components[product] = r[2]
        # In theory, there should be some way to set a variable on Bug
        # such that it contains attributes for all the keys listed in the
        # getBug call.  This isn't it, though.
        #{'methodName':'bugzilla.getBug','params':(1,self.user,self.password)}
        #Bug.__slots__ = r[3].keys()
                 

    #---- Methods for reading bugs and bug info

    # Return raw dicts
    def _getbug(self,id):
        '''Return a dict of full bug info for the given bug id'''
        return self._proxy.bugzilla.getBug(id, self.user, self.password)
    def _getbugsimple(self,id):
        '''Return a short dict of simple bug info for the given bug id'''
        return self._proxy.bugzilla.getBugSimple(id, self.user, self.password)
    def _getbugs(self,idlist):
        '''Like _getbug, but takes a list of ids and returns a corresponding
        list of bug objects. Uses multicall for awesome speed.'''
        mc = self._multicall()
        for id in idlist:
            mc._getbug(id)
        return mc.run()
        # I sure hope mc gets garbage-collected here.
    def _getbugssimple(self,idlist):
        '''Like _getbugsimple, but takes a list of ids and returns a
        corresponding list of bug objects. Uses multicall for awesome speed.'''
        mc = self._multicall()
        for id in idlist:
            mc._getbugsimple(id)
        return mc.run()
    def _query(self,query):
        '''Query bugzilla and return a list of matching bugs.
        query must be a dict with fields like those in in querydata['fields'].

        Returns a dict like this: {'bugs':buglist,
                                   'displaycolumns':columnlist,
                                   'sql':querystring}
        
        buglist is a list of dicts describing bugs. You can specify which 
        columns/keys will be listed in the bugs by setting 'column_list' in
        the query; otherwise the default columns are used (see the list in
        querydefaults['default_column_list']). The list of columns will be
        in 'displaycolumns', and the SQL query used by this query will be in
        'sql'. 
        ''' 
        return self._proxy.bugzilla.runQuery(query,self.user,self.password)

    # these return Bug objects 
    def getbug(self,id):
        '''Return a Bug object with the full complement of bug data
        already loaded.'''
        return Bug(bugzilla=self,dict=self._getbug(id))
    def getbugsimple(self,id):
        '''Return a Bug object given bug id, populated with simple info'''
        return Bug(bugzilla=self,dict=self._getbugsimple(id))
    def getbugs(self,idlist):
        '''Return a list of Bug objects with the full complement of bug data
        already loaded.'''
        return [Bug(bugzilla=self,dict=b) for b in self._getbugs(idlist)]
    def getbugssimple(self,idlist):
        '''Return a list of Bug objects for the given bug ids, populated with
        simple info'''
        return [Bug(bugzilla=self,dict=b) for b in self._getbugssimple(idlist)]
    def query(self,query):
        '''Query bugzilla and return a list of matching bugs.
        query must be a dict with fields like those in in querydata['fields'].

        Returns a list of Bug objects.
        '''
        r = self._query(query)
        return [Bug(bugzilla=self,dict=b) for b in r['bugs']]

    def query_comments(self,product,version,component,string,matchtype='allwordssubstr'):
        '''Convenience method - query for bugs filed against the given
        product, version, and component whose comments match the given string.
        matchtype specifies the type of match to be done. matchtype may be
        any of the types listed in querydefaults['long_desc_type_list'], e.g.:
        ['allwordssubstr','anywordssubstr','substring','casesubstring',
         'allwords','anywords','regexp','notregexp']
        Return value is the same as with query().
        '''
        q = {'product':product,'version':version,'component':component,
             'long_desc':string,'long_desc_type':matchtype}
        return self.query(q)

    #---- Methods for modifying existing bugs.

    # Most of these will probably also be available as Bug methods, e.g.:
    # Bugzilla.setstatus(id,status) ->
    #   Bug.setstatus(status): self.bugzilla.setstatus(self.bug_id,status)

    def _addcomment(self,id,comment,private=False,
                   timestamp='',worktime='',bz_gid=''):
        '''Add a comment to the bug with the given ID. Other optional 
        arguments are as follows:
            private:   if True, mark this comment as private.
            timestamp: comment timestamp, in the form "YYYY-MM-DD HH:MM:SS"
            worktime:  amount of time spent on this comment (undoc in upstream)
            bz_gid:    if present, and the entire bug is *not* already private
                       to this group ID, this comment will be marked private.
        '''
        return self._proxy.bugzilla.addComment(id,comment,
                   self.user,self.password,private,timestamp,worktime,bz_gid)
    
    def _setstatus(self,id,status,comment='',private=False):
        '''Set the status of the bug with the given ID. You may optionally
        include a comment to be added, and may further choose to mark that
        comment as private.
        The status may be anything from querydefaults['bug_status_list'].
        Common statuses: 'NEW','ASSIGNED','MODIFIED','NEEDINFO'
        Less common: 'VERIFIED','ON_DEV','ON_QA','REOPENED'
        'CLOSED' is not valid with this method; use closebug() instead.
        '''
        return self._proxy.bugzilla.setstatus(id,status,
                self.user,self.password,comment,private)

    def _closebug(self,id):
        #closeBug($bugid, $new_resolution, $username, $password, $dupeid, $new_fixed_in, $comment, $isprivate)
        # if new_resolution is 'DUPLICATE', dupeid is not optional
        # new_fixed_id, comment, isprivate are optional
        raise NotImplementedError

    def _setassignee(self,id,assignee):
        #changeAssignment($id, $data, $username, $password)
        #data: 'assigned_to','reporter','qa_contact','comment'
        #returns: [$id, $mailresults]
        raise NotImplementedError

    def _updatedeps(self,id,deplist):
        #updateDepends($bug_id,$data,$username,$password,$nodependencyemail)
        #data: 'blocked'=>id,'dependson'=>id,'action' => ('add','remove')
        raise NotImplementedError

    def _updatecc(self,id,cclist,action,comment='',nomail=False):
        '''Updates the CC list using the action and account list specified.
        action may be 'add', 'remove', or 'makeexact'.
        comment specifies an optional comment to add to the bug.
        if mail is True, email will be generated for this change.
        '''
        data = {'id':id, 'action':action, 'cc':','.join(cclist),
                'comment':comment, 'nomail':nomail}
        return self._proxy.bugzilla.updateCC(data,self.user,self.password)

    def _updatewhiteboard(self,id,text,which,action):
        '''Update the whiteboard given by 'which' for the given bug.
        performs the given action (which may be 'append',' prepend', or 
        'overwrite') using the given text.'''
        data = {'type':which,'text':text,'action':action}
        return self._proxy.bugzilla.updateWhiteboard(id,data,self.user,self.password)

    # TODO: flag handling?

    #---- Methods for working with attachments

    def __attachment_encode(self,fh):
        '''Return the contents of the file-like object fh in a form
        appropriate for attaching to a bug in bugzilla.'''
        # Read data in chunks so we don't end up with two copies of the file
        # in RAM.
        chunksize = 3072 # base64 encoding wants input in multiples of 3
        data = ''
        chunk = fh.read(chunksize)
        while chunk:
            # we could use chunk.encode('base64') but that throws a newline
            # at the end of every output chunk, which increases the size of
            # the output.
            data = data + base64.b64encode(chunk)
            chunk = fh.read(chunksize)
        return data

    def attachfile(self,id,attachfile,description,**kwargs):
        '''Attach a file to the given bug ID. Returns the ID of the attachment
        or raises xmlrpclib.Fault if something goes wrong.
        attachfile may be a filename (which will be opened) or a file-like
        object, which must provide a 'read' method. If it's not one of these,
        this method will raise a TypeError.
        description is the short description of this attachment.
        Optional keyword args are as follows:
            filename:  this will be used as the filename for the attachment.
                       REQUIRED if attachfile is a file-like object with no
                       'name' attribute, otherwise the filename or .name
                       attribute will be used.
            comment:   An optional comment about this attachment.
            isprivate: Set to True if the attachment should be marked private.
            ispatch:   Set to True if the attachment is a patch.
            contenttype: The mime-type of the attached file. Defaults to
                         application/octet-stream if not set. NOTE that text
                         files will *not* be viewable in bugzilla unless you 
                         remember to set this to text/plain. So remember that!
        '''
        if isinstance(attachfile,str):
            f = open(attachfile)
        elif hasattr(attachfile,'read'):
            f = attachfile
        else:
            raise TypeError, "attachfile must be filename or file-like object"
        kwargs['description'] = description
        if 'filename' not in kwargs:
            kwargs['filename'] = os.path.basename(f.name)
        # TODO: guess contenttype?
        if 'contenttype' not in kwargs:
            kwargs['contenttype'] = 'application/octet-stream'
        kwargs['data'] = self.__attachment_encode(f)
        (attachid, mailresults) = server._proxy.bugzilla.addAttachment(id,kwargs,self.user,self.password)
        return attachid

    def openattachment(self,attachid):
        '''Get the contents of the attachment with the given attachment ID.
        Returns a file-like object.'''
        att_uri = self._url.replace('xmlrpc.cgi','attachment.cgi')
        att_uri = att_uri + '?%i' % attachid
        att = urllib2.urlopen(att_uri)
        # RFC 2183 defines the content-disposition header, if you're curious
        disp = att.headers['content-disposition'].split(';')
        [filename_parm] = [i for i in disp if i.strip().startswith('filename=')]
        (dummy,filename) = filename_parm.split('=')
        # RFC 2045/822 defines the grammar for the filename value, but
        # I think we just need to remove the quoting. I hope.
        att.name = filename.strip('"')
        # Hooray, now we have a file-like object with .read() and .name
        return att

    #---- createbug - big complicated call to create a new bug

    def createbug(self,**kwargs):
        '''Create a bug with the given info. Returns the bug ID.'''
        raise NotImplementedError


class CookieTransport(xmlrpclib.Transport):
    '''A subclass of xmlrpclib.Transport that supports cookies.'''
    cookiejar = None
    scheme = 'http'

    # Cribbed from xmlrpclib.Transport.send_user_agent 
    def send_cookies(self, connection, cookie_request):
        if self.cookiejar is None:
            self.cookiejar = cookielib.CookieJar()
        elif self.cookiejar:
            # Let the cookiejar figure out what cookies are appropriate
            self.cookiejar.add_cookie_header(cookie_request)
            # Pull the cookie headers out of the request object...
            cookielist=list()
            for h,v in cookie_request.header_items():
                if h.startswith('Cookie'):
                    cookielist.append([h,v])
            # ...and put them over the connection
            for h,v in cookielist:
                connection.putheader(h,v)

    # This is the same request() method from xmlrpclib.Transport,
    # with a couple additions noted below
    def request(self, host, handler, request_body, verbose=0):
        h = self.make_connection(host)
        if verbose:
            h.set_debuglevel(1)

        # ADDED: construct the URL and Request object for proper cookie handling
        request_url = "%s://%s/" % (self.scheme,host)
        cookie_request  = urllib2.Request(request_url) 

        self.send_request(h,handler,request_body)
        self.send_host(h,host) 
        self.send_cookies(h,cookie_request) # ADDED. creates cookiejar if None.
        self.send_user_agent(h)
        self.send_content(h,request_body)

        errcode, errmsg, headers = h.getreply()

        # ADDED: parse headers and get cookies here
        # fake a response object that we can fill with the headers above
        class CookieResponse:
            def __init__(self,headers): self.headers = headers
            def info(self): return self.headers
        cookie_response = CookieResponse(headers)
        # Okay, extract the cookies from the headers
        self.cookiejar.extract_cookies(cookie_response,cookie_request)
        # And write back any changes
        if hasattr(self.cookiejar,'save'):
            self.cookiejar.save(self.cookiejar.filename)

        if errcode != 200:
            raise xmlrpclib.ProtocolError(
                host + handler,
                errcode, errmsg,
                headers
                )

        self.verbose = verbose

        try:
            sock = h._conn.sock
        except AttributeError:
            sock = None

        return self._parse_response(h.getfile(), sock)

class SafeCookieTransport(xmlrpclib.SafeTransport,CookieTransport):
    '''SafeTransport subclass that supports cookies.'''
    scheme = 'https'
    request = CookieTransport.request

class Bug(object):
    '''A container object for a bug report. Requires a Bugzilla instance - 
    every Bug is on a Bugzilla, obviously.
    Optional keyword args:
        dict=DICT - populate attributes with the result of a getBug() call
        bug_id=ID - if dict does not contain bug_id, this is required before
                    you can read any attributes or make modifications to this
                    bug.
    Note that modifying a Bug will not update the data attributes - you can 
    call refresh() to do this.
    '''
    # TODO: Implement an 'autorefresh' attribute that causes update methods
    # to run as multicalls which fetch the newly-updated data afterward.
    def __init__(self,bugzilla,**kwargs):
        self.bugzilla = bugzilla
        if 'dict' in kwargs and kwargs['dict']:
            self.__dict__.update(kwargs['dict'])
        if 'bug_id' in kwargs:
            setattr(self,'bug_id',kwargs['bug_id'])

    def __str__(self):
        '''Return a simple string representation of this bug'''
        if 'short_short_desc' in self.__dict__:
            desc = self.short_short_desc
        else:
            desc = self.short_desc
        return "#%-6s %-10s - %s - %s" % (self.bug_id,self.bug_status,
                                          self.assigned_to,desc)

    def __getattr__(self,name):
        if name not in ('__members__','__methods__','trait_names',
                '_getAttributeNames') and not name.endswith(')'):
            # FIXME: that .endswith hack is an extremely stupid way to figure
            # out if we're checking on a method call. Find a smarter one!
            if not 'bug_id' in self.__dict__:
                raise AttributeError
            #print "Bug %i missing %s - loading" % (self.bug_id,name)
            self.refresh()
            if name in self.__dict__:
                return self.__dict__[name]
        raise AttributeError

    def refresh(self):
        '''Refresh all the data in this Bug.'''
        r = self.bugzilla._getbug(self.bug_id)
        self.__dict__.update(r)

    def _dowhiteboard(self,text,which,action):
        '''Actually does the updateWhiteboard call to perform the given action
        (append,prepend,overwrite) with the given text on the given whiteboard
        for the given bug.'''
        self.bugzilla._updatewhiteboard(self.bug_id,text,which,action)
    def getwhiteboard(self,which='status'):
        '''Get the current value of the whiteboard specified by 'which'.
        Known whiteboard names: 'status','internal','devel','qa'.
        Defaults to the 'status' whiteboard.
        '''
        return getattr(self,"%s_whiteboard" % which)
    def appendwhiteboard(self,text,which='status'):
        '''Append the given text (with a space before it) to the given 
        whiteboard. Defaults to using status_whiteboard.'''
        self._dowhiteboard(text,which,'append')
    def prependwhiteboard(self,text,which='status'):
        '''Prepend the given text (with a space following it) to the given
        whiteboard. Defaults to using status_whiteboard.'''
        self._dowhiteboard(text,which,'prepend')
    def setwhiteboard(self,text,which='status'):
        '''Overwrites the contents of the given whiteboard with the given text.
        Defaults to using status_whiteboard.'''
        self._dowhiteboard(text,which,'overwrite')
    def addtag(self,tag,which='status'):
        '''Adds the given tag to the given bug.'''
        whiteboard = self.getwhiteboard(which)
        if whiteboard:
            self.appendwhiteboard(tag,which)
        else:
            self.setwhiteboard(tag,which)
    def gettags(self,which='status'):
        '''Get a list of tags (basically just whitespace-split the given
        whiteboard)'''
        return self.getwhiteboard(which).split()
    def deltag(self,tag,which='status'):
        '''Removes the given tag from the given bug.'''
        tags = self.gettags(which)
        tags.remove(tag)
        self.setwhiteboard(' '.join(tags),which)
# TODO: add a sync() method that writes the changed data in the Bug object
# back to Bugzilla. Someday.
