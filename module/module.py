#!/usr/bin/python

# -*- coding: utf-8 -*-

# Copyright (C) 2009-2014:
#    Gabes Jean, naparuba@gmail.com
#    Gerhard Lausser, Gerhard.Lausser@consol.de
#    Gregory Starck, g.starck@gmail.com
#    Hartmut Goebel, h.goebel@goebel-consult.de
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.

"""
This Class is a plugin for the Shinken Broker. It is in charge
to get brok and recreate real objects, and propose a Web interface :)
"""

import traceback
import sys
import os
import time
import traceback
import select
import threading
import base64
import cPickle
import imp
import urllib
import hashlib

from shinken.basemodule import BaseModule
from shinken.message import Message
from shinken.misc.regenerator import Regenerator
from shinken.log import logger
from shinken.modulesctx import modulesctx
from shinken.modulesmanager import ModulesManager
from shinken.daemon import Daemon
from shinken.util import safe_print, to_bool
from shinken.misc.filter  import only_related_to
from shinken.misc.sorter import hst_srv_sort, last_state_change_earlier

# Local import
from shinken.misc.datamanager import datamgr
from helper import helper
from config_parser import config_parser
from lib.bottle import Bottle, run, static_file, view, route, request, response, template, redirect

# Debug
import lib.bottle as bottle
bottle.debug(True)

# Import bottle lib to make bottle happy
bottle_dir = os.path.abspath(os.path.dirname(bottle.__file__))
sys.path.insert(0, bottle_dir)

# Look at the webui module root dir too
webuimod_dir = os.path.abspath(os.path.dirname(__file__))
htdocs_dir = os.path.join(webuimod_dir, 'htdocs')

properties = {
    'daemons': ['broker', 'scheduler'],
    'type': 'webui',
    'phases': ['running'],
    'external': True,
    }


import os,sys
from webui.config_parser import config_parser
# plugin_name = os.path.splitext(os.path.basename(__file__))[0]
try:
    currentdir = os.path.dirname(os.path.realpath(__file__))
    configuration_file = "%s/%s" % (currentdir, 'menu.cfg')
    logger.warning("WebUI configuration file: %s" % (configuration_file))
    # True to allow duplicate entries ...
    scp = config_parser('#', '=', True)
    params = scp.parse_config(configuration_file)

    logger.debug("WebUI, configuration loaded.")
    logger.info("WebUI configuration, sidebar menu: %s" % (params['sidebar_menu']))
    logger.info("WebUI configuration, hosts filtered: %s" % (params['hosts_filter']))
except Exception, exp:
    logger.warning("WebUI, configuration file (%s) not available: %s" % (configuration_file, str(exp)))
    
# called by the plugin manager to get an instance
def get_instance(plugin):
    # Only add template if we CALL webui
    bottle.TEMPLATE_PATH.append(os.path.join(webuimod_dir, 'views'))
    bottle.TEMPLATE_PATH.append(webuimod_dir)

    print "Get a WebUI instancefor plugin %s" % plugin.get_name()

    instance = Webui_broker(plugin)
    return instance


# Class for the WebUI Broker
class Webui_broker(BaseModule, Daemon):
    def __init__(self, modconf):
        BaseModule.__init__(self, modconf)

        self.plugins = []

        self.serveropts = {}
        umask = getattr(modconf, 'umask', None)
        if umask != None: 
            self.serveropts['umask'] = int(umask)
        bindAddress = getattr(modconf, 'bindAddress', None)
        if bindAddress:
            self.serveropts['bindAddress'] = str(bindAddress)

        self.port = int(getattr(modconf, 'port', '7767'))
        self.http_port = int(getattr(modconf, 'http_port', '7766'))
        self.host = getattr(modconf, 'host', '0.0.0.0')
        self.show_skonf = int(getattr(modconf, 'show_skonf', '1'))
        self.auth_secret = getattr(modconf, 'auth_secret', 'secret').encode('utf8', 'replace')
        self.play_sound = to_bool(getattr(modconf, 'play_sound', '0'))
        self.http_backend = getattr(modconf, 'http_backend', 'auto')
        self.login_text = getattr(modconf, 'login_text', None)
        self.company_logo = getattr(modconf, 'company_logo', 'logo.png')
        self.gravatar = to_bool(getattr(modconf, 'gravatar', '0'))
        self.allow_html_output = to_bool(getattr(modconf, 'allow_html_output', '0'))
        self.max_output_length = int(getattr(modconf, 'max_output_length', '100'))
        self.refresh_period = int(getattr(modconf, 'refresh_period', '60'))
        self.manage_acl = to_bool(getattr(modconf, 'manage_acl', '1'))
        self.remote_user_enable = getattr(modconf, 'remote_user_enable', '0')
        self.remote_user_variable = getattr(modconf, 'remote_user_variable', 'X_REMOTE_USER')

        # Load the share dir and make it an absolute path
        self.share_dir = getattr(modconf, 'share_dir', 'share')
        self.share_dir = os.path.abspath(self.share_dir)
        logger.info("[%s] Share dir: %s", self.name, self.share_dir)
        # Load the photo dir and make it an absolute path
        self.photo_dir = getattr(modconf, 'photo_dir', 'photos')
        self.photo_dir = os.path.abspath(self.photo_dir)
        logger.info("[%s] Photo dir: %s", self.name, self.photo_dir)

        self.embeded_graph = to_bool(getattr(modconf, 'embeded_graph', '0'))

        # Look for an additional pages dir
        self.additional_plugins_dir = getattr(modconf, 'additional_plugins_dir', '')
        if self.additional_plugins_dir:
            self.additional_plugins_dir = os.path.abspath(self.additional_plugins_dir)
        
        self.timezone = getattr(modconf, 'timezone', 'Europe/Paris')
        if self.timezone:
            logger.info("[%s] Setting our timezone to %s", self.name, self.timezone)
            os.environ['TZ'] = self.timezone
            time.tzset()
        logger.info("[%s] parameter timezone: %s", self.name, self.timezone)

        self.sidebar_menu = None
        self.menu_items = []
        if params['sidebar_menu'] and params['sidebar_menu'] is not None:
            self.sidebar_menu = params['sidebar_menu']
            for (menu) in self.sidebar_menu: 
                menu = [item.strip() for item in menu.split(',')]
                self.menu_items.append(menu[0])
        logger.info("[%s] parameter sidebar_menu: %s", self.name, self.sidebar_menu)
        
        self.hosts_filter = []
        if 'hosts_filter' in params and params['hosts_filter'] is not None:
            self.hosts_filter = params['hosts_filter']
        logger.info("[%s] parameter host_filter: %s", self.name, self.hosts_filter)
            
        # Web UI information
        self.app_version = getattr(modconf, 'about_version', '1.1.0-dev.3 - Contis')
        self.app_copyright = getattr(modconf, 'about_copyright', 'License GNU AGPL as published by the FSF, minimum version 3 of the License.')
        self.app_release = getattr(modconf, 'about_release', 'Bootstrap 3 version')
        
        # We will save all widgets
        self.widgets = {}
        # We need our regenerator now (before main) so if we are in a scheduler,
        # rg will be able to skip some broks
        self.rg = Regenerator()

        self.bottle = bottle
    
    
    # We check if the photo directory exists. If not, try to create it
    def check_photo_dir(self):
        print "Checking photo path", self.photo_dir
        if not os.path.exists(self.photo_dir):
            print "Trying to create photo dir", self.photo_dir
            try:
                os.mkdir(self.photo_dir)
            except Exception, exp:
                print "Photo dir creation failed", exp


    # Called by Broker so we can do init stuff
    # TODO: add conf param to get pass with init
    # Conf from arbiter!
    def init(self):
        logger.info("[%s] Initializing ...", self.name)
        self.rg.load_external_queue(self.from_q)


    # This is called only when we are in a scheduler
    # and just before we are started. So we can gain time, and
    # just load all scheduler objects without fear :) (we
    # will be in another process, so we will be able to hack objects
    # if need)
    def hook_pre_scheduler_mod_start(self, sched):
        print "pre_scheduler_mod_start::", sched.__dict__
        self.rg.load_from_scheduler(sched)


    # In a scheduler we will have a filter of what we really want as a brok
    def want_brok(self, b):
        return self.rg.want_brok(b)


    def main(self):
        self.set_proctitle(self.name)

        self.log = logger
        self.log.load_obj(self)

        # Daemon like init
        self.debug_output = []
        self.modules_dir = modulesctx.get_modulesdir()
        self.modules_manager = ModulesManager('webui', self.find_modules_path(), [])
        self.modules_manager.set_modules(self.modules)
        # We can now output some previously silenced debug output
        self.do_load_modules()
        for inst in self.modules_manager.instances:
            f = getattr(inst, 'load', None)
            if f and callable(f):
                f(self)


        for s in self.debug_output:
            print s
        del self.debug_output

        self.check_photo_dir()
        self.datamgr = datamgr
        datamgr.load(self.rg)
        self.helper = helper

        self.request = request
        self.response = response
        self.template_call = template
        
        try:
            #import cProfile
            #cProfile.runctx('''self.do_main()''', globals(), locals(),'/tmp/webui.profile')
            self.do_main()
        except Exception, exp:
            msg = Message(id=0, type='ICrash', data={'name': self.get_name(), 'exception': exp, 'trace': traceback.format_exc()})
            self.from_q.put(msg)
            # wait 2 sec so we know that the broker got our message, and die
            time.sleep(2)
            raise


    # A plugin send us en external command. We just put it
    # in the good queue
    def push_external_command(self, e):
        logger.info("[%s] Got an external command: %s", self.name, e.__dict__)
        try:
            self.from_q.put(e)
        except Exception, exp:
            logger.error("[%s] External command push, exception: %s", self.name, str(exp))


    # Real main function
    def do_main(self):
        # I register my exit function
        self.set_exit_handler()

        # We will protect the operations on
        # the non read+write with a lock and
        # 2 int
        self.global_lock = threading.RLock()
        self.nb_readers = 0
        self.nb_writers = 0

        self.data_thread = None

        # Check if the view dir really exist
        if not os.path.exists(bottle.TEMPLATE_PATH[0]):
            logger.error("The view path do not exist at %s" % bottle.TEMPLATE_PATH)
            sys.exit(2)

        # First load the additonal plugins so they will have the lead on
        # URI routes
        if self.additional_plugins_dir:
            self.load_plugins(self.additional_plugins_dir)

        # Modules can also override some views if need
        for inst in self.modules_manager.instances:
            f = getattr(inst, 'get_webui_plugins_path', None)
            if f and callable(f):
                mod_plugins_path = os.path.abspath(f(self))
                self.load_plugins(mod_plugins_path)
                

        # Then look at the plugins into core and load all we can there
        core_plugin_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'plugins')
        self.load_plugins(core_plugin_dir)

        # Declare the whole app static files AFTER the plugin ones
        self.declare_common_static()

        # Launch the data thread"
        self.data_thread = threading.Thread(None, self.manage_brok_thread, 'datathread')
        self.data_thread.start()
        # TODO: look for alive and killing

        # Ok, you want to know why we are using a data thread instead of
        # just call for a select with q._reader, the underlying file
        # handle of the Queue()? That's just because under Windows, select
        # only manage winsock (so network) file descriptor! What a shame!
        logger.info("[%s] starting Web UI server ...", self.name)
        srv = run(host=self.host, port=self.port, server=self.http_backend, **self.serveropts)

        # ^ IMPORTANT ^
        # We are not managing the lock at this
        # level because we got 2 types of requests:
        # static images/css/js: no need for lock
        # pages: need it. So it's managed at a
        # function wrapper at loading pass


    # It's the thread function that will get broks
    # and update data. Will lock the whole thing
    # while updating
    def manage_brok_thread(self):
        logger.debug("[%s] manage_brok_thread start ...", self.name)

        while True:
            # DBG: t0 = time.time()
            # DBG: print "WEBUI :: GET START"
            l = self.to_q.get()
            # DBG: t1 = time.time()
            # DBG: print "WEBUI :: GET FINISH with", len(l), "in ", t1 - t0

            # try to relaunch dead module (like mongo one when mongo is not available at startup for example)
            self.check_and_del_zombie_modules()

            logger.debug("[%s] manage_brok_thread got %d broks", self.name, len(l))
            for b in l:
                b.prepare()
                self.wait_for_no_readers()
                try:
                    # print "Got data lock, manage brok"
                    # DBG: t0 = time.time()
                    self.rg.manage_brok(b)
                    # DBG: times[b.type] += time.time() - t0

                    for mod in self.modules_manager.get_internal_instances():
                        try:
                            mod.manage_brok(b)
                        except Exception, exp:
                            print exp.__dict__
                            logger.warning("[%s] The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                            logger.debug("[%s] Exception type: %s" % (self.name, type(exp)))
                            logger.debug("Back trace of this kill: %s" % (traceback.format_exc()))
                            self.modules_manager.set_to_restart(mod)
                except Exception, exp:
                    logger.error("[%s] manage_brok_thread exception", self.name)
                    msg = Message(id=0, type='ICrash', data={'name': self.get_name(), 'exception': exp, 'trace': traceback.format_exc()})
                    self.from_q.put(msg)
                    # wait 2 sec so we know that the broker got our message, and die
                    time.sleep(2)
                    # No need to raise here, we are in a thread, exit!
                    os._exit(2)
                finally:
                    # logger.error("[%s] manage_brok_thread finally", self.name)
                    # We can remove us as a writer from now. It's NOT an atomic operation
                    # so we REALLY not need a lock here (yes, I try without and I got
                    # a not so accurate value there....)
                    self.global_lock.acquire()
                    self.nb_writers -= 1
                    self.global_lock.release()

        logger.debug("[%s] manage_brok_thread end ...", self.name)


    def load_plugin(self, fdir, plugin_dir):
        logger.debug("[%s] loading plugin %s ..." % (self.name, fdir))
        try:
            # Put the full qualified path of the module we want to load
            # for example we will give  webui/plugins/eltdetail/
            mod_path = os.path.join(plugin_dir, fdir)
            # Then we load the eltdetail.py inside this directory
            m = imp.load_module('%s' % (fdir), *imp.find_module(fdir, [mod_path]))
            m_dir = os.path.abspath(os.path.dirname(m.__file__))
            sys.path.append(m_dir)

            logger.debug("[%s] loaded plugin %s" % (self.name, fdir))
            pages = m.pages
            for (f, entry) in pages.items():
                routes = entry.get('routes', None)
                v = entry.get('view', None)
                static = entry.get('static', False)
                widget_lst = entry.get('widget', [])
                widget_desc = entry.get('widget_desc', None)
                widget_name = entry.get('widget_name', None)
                widget_picture = entry.get('widget_picture', None)

                # IMPORTANT: apply VIEW BEFORE route!
                if v:
                    f = view(v)(f)

                # Maybe there is no route to link, so pass
                if routes:
                    for r in routes:
                        method = entry.get('method', 'GET')

                        # Ok, we will just use the lock for all
                        # plugin page, but not for static objects
                        # so we set the lock at the function level.
                        lock_version = self.lockable_function(f)
                        f = route(r, callback=lock_version, method=method)

                # If the plugin declare a static entry, register it
                # and remember: really static! because there is no lock
                # for them!
                if static:
                    self.add_static(fdir, m_dir)

                # It's a valid widget entry if it got all data, and at least one route
                # ONLY the first route will be used for Add!
                #print "Should I load a widget?",widget_name, widget_desc, widget_lst!=[], routes
                if widget_name and widget_desc and widget_lst != [] and routes:
                    for place in widget_lst:
                        if place not in self.widgets:
                            self.widgets[place] = []
                        w = {'widget_name': widget_name, 'widget_desc': widget_desc, 'base_uri': routes[0],
                             'widget_picture': widget_picture}
                        self.widgets[place].append(w)

            # And we add the views dir of this plugin in our TEMPLATE
            # PATH
            bottle.TEMPLATE_PATH.append(os.path.join(m_dir, 'views'))

            # And finally register me so the pages can get data and other
            # useful stuff
            m.app = self


        except Exception, exp:
            logger.error("[%s] loading plugin %s, exception: %s" % (self.name, fdir, exp))
        

    # Here we will load all plugins (pages) under the webui/plugins
    # directory. Each one can have a page, views and htdocs dir that we must
    # route correctly
    def load_plugins(self, plugin_dir):
        logger.info("[%s] load plugins directory: %s" % (self.name, plugin_dir))

        # Load plugin directories
        if not os.path.exists(plugin_dir):
            return
        
        plugin_dirs = [fname for fname in os.listdir(plugin_dir)
                       if os.path.isdir(os.path.join(plugin_dir, fname))]

        sys.path.append(plugin_dir)
        # We try to import plugins
        for fdir in plugin_dirs:
            self.load_plugin(fdir, plugin_dir)
    

    def add_static(self, fdir, m_dir):
        static_route = '/static/' + fdir + '/:path#.+#'
        print "Declaring static route", static_route

        def plugin_static(path):
            print "Ask %s and give %s" % (path, os.path.join(m_dir, 'htdocs'))
            return static_file(path, root=os.path.join(m_dir, 'htdocs'))
        route(static_route, callback=plugin_static)


    # It will say if we can launch a page rendering or not.
    # We can only if there is no writer running from now
    def wait_for_no_writers(self):
        can_run = False
        while True:
            self.global_lock.acquire()
            # We will be able to run
            if self.nb_writers == 0:
                # Ok, we can run, register us as readers
                self.nb_readers += 1
                self.global_lock.release()
                break
            # Oups, a writer is in progress. We must wait a bit
            self.global_lock.release()
            # Before checking again, we should wait a bit
            # like 1ms
            time.sleep(0.001)


    # It will say if we can launch a brok management or not
    # We can only if there is no readers running from now
    def wait_for_no_readers(self):
        start = time.time()
        while True:
            self.global_lock.acquire()
            # We will be able to run
            if self.nb_readers == 0:
                # Ok, we can run, register us as writers
                self.nb_writers += 1
                self.global_lock.release()
                break
            # Ok, we cannot run now, wait a bit
            self.global_lock.release()
            # Before checking again, we should wait a bit
            # like 1ms
            time.sleep(0.001)
            # We should warn if we cannot update broks
            # for more than 30s because it can be not good
            if time.time() - start > 30:
                print "WARNING: we are in lock/read since more than 30s!"
                start = time.time()


    # We want a lock manager version of the plugin functions
    def lockable_function(self, f):
        print "We create a lock version of", f

        def lock_version(**args):
            self.wait_for_no_writers()
            t = time.time()
            try:
                return f(**args)
            finally:
                print "rendered in", time.time() - t
                # We can remove us as a reader from now. It's NOT an atomic operation
                # so we REALLY not need a lock here (yes, I try without and I got
                # a not so accurate value there....)
                self.global_lock.acquire()
                self.nb_readers -= 1
                self.global_lock.release()
        print "The lock version is", lock_version
        return lock_version


    def declare_common_static(self):
        @route('/static/photos/:path#.+#')
        def give_photo(path):
            # If the file really exist, give it. If not, give a dummy image.
            if os.path.exists(os.path.join(self.photo_dir, path+'.png')):
                return static_file(path+'.png', root=self.photo_dir)
            else:
                return static_file('images/user.png', root=htdocs_dir)

        # Route static files css files
        @route('/static/:path#.+#')
        def server_static(path):
            # By default give from the root in bottle_dir/htdocs. If the file is missing,
            # search in the share dir
            root = htdocs_dir
            p = os.path.join(root, path)
            if not os.path.exists(p):
                root = self.share_dir
            return static_file(path, root=root)

        # And add the favicon ico too
        @route('/favicon.ico')
        def give_favicon():
            return static_file('favicon.ico', root=os.path.join(htdocs_dir, 'images'))

            
    def check_auth(self, user, password):
        logger.info("[%s] Checking authentication for user: %s" % (self.name, user))
        c = self.datamgr.get_contact(user)
        if not c:
            logger.error("[%s] You need to have a contact having the same name as your user: %s" % (self.name, user))
            return False

        is_ok = False
        for mod in self.modules_manager.get_internal_instances():
            try:
                f = getattr(mod, 'check_auth', None)
                logger.debug("[%s] Check auth with: %s, for %s" % (self.name, mod.get_name(), user))
                if f and callable(f):
                    r = f(user, password)
                    if r:
                        is_ok = True
                        # No need for other modules
                        logger.info("[%s] User '%s' is authenticated by %s" % (self.name, user, mod.get_name()))
                        break
            except Exception, exp:
                print exp.__dict__
                logger.warning("[%s] The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                logger.debug("[%s] Exception type: %s" % (self.name, type(exp)))
                logger.debug("Back trace of this kill: %s" % (traceback.format_exc()))
                self.modules_manager.set_to_restart(mod)

        # Ok if we got a real contact, and if a module auth it
        return (is_ok and c is not None)

        
    def get_user_auth(self, allow_anonymous=False):
        # First we look for the user sid
        # so we bail out if it's a false one
        user_name = self.request.get_cookie("user", secret=self.auth_secret)

        # If we cannot check the cookie, bailout ... 
        if not allow_anonymous and not user_name:
            return None
            
        # Allow anonymous access if requested and anonymous contact exists ...
        if allow_anonymous:
            c = self.datamgr.get_contact('anonymous')
            if c:
                return c

        c = self.datamgr.get_contact(user_name)
        return c

        
    def checkauth(self):
        user = self.get_user_auth()
        if not user:
            self.bottle.redirect("/user/login")
        else:
            return user
            
    def get_gravatar(self, email, size=64, default='404'):
        """
        Given an email, returns a gravatar url for that email.
        
        From : https://fr.gravatar.com/site/implement/images/

        :param basestring email:
        :rtype: basestring
        :return: The gravatar url for the given email.
        """
        parameters = { 's' : size, 'd' : default}
        url = "https://secure.gravatar.com/avatar/%s?%s" % (hashlib.md5(email.lower()).hexdigest(), urllib.urlencode(parameters))
        return url


    # Try to got for an element the graphs uris from modules
    # The source variable describes the source of the calling. Are we displaying 
    # graphs for the element detail page (detail), or a widget in the dashboard (dashboard) ?
    def get_graph_uris(self, elt, graphstart, graphend, source = 'detail'):
        uris = []
        for mod in self.modules_manager.get_internal_instances():
            try:
                logger.debug("[%s] module %s, get_graph_uris", self.name, mod)
                f = getattr(mod, 'get_graph_uris', None)
                #safe_print("Get graph uris ", f, "from", mod.get_name())
                if f and callable(f):
                    r = f(elt, graphstart, graphend, source)
                    uris.extend(r)
            except Exception, exp:
                logger.warning("[%s] The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                logger.debug("[%s] Exception type: %s" % (self.name, type(exp)))
                logger.debug("Back trace of this kill: %s" % (traceback.format_exc()))
                self.modules_manager.set_to_restart(mod)

        #safe_print("Will return", uris)
        # Ok if we got a real contact, and if a module auth it
        return uris

    def get_graph_img_src(self,uri,link):
        url=uri
        lk=link
        if self.embeded_graph:
            data = urllib.urlopen(uri, 'rb').read().encode('base64').replace('\n', '')
            url="data:image/png;base64,{0}".format(data)
            lk=''
        return (url,lk)
        
    # Maybe a page want to warn if there is no module that is able to give user preference?
    def has_user_preference_module(self):
        for mod in self.modules_manager.get_internal_instances():
            f = getattr(mod, 'get_ui_user_preference', None)
            if f and callable(f):
                return True
        return False
        

    # Try to get user's preferences for Web UI plugins ...
    def get_user_preference(self, user, key=None, default=None):
        logger.debug("[%s] Fetching user preference for: %s / %s", self.name, user.get_name(), key)

        for mod in self.modules_manager.get_internal_instances():
            try:
                logger.debug("[%s] Trying to get preference %s from %s", self.name, key, mod.get_name())
                f = getattr(mod, 'get_ui_user_preference', None)
                if f and callable(f):
                    r = f(user, key)
                    logger.debug("[%s] Found '%s', %s = %s", self.name, user.get_name(), key, r)
                    if r is not None:
                        return r
            except Exception, exp:
                logger.warning("[%s] The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                logger.warning("[%s] Exception type: %s" % (self.name, type(exp)))
                logger.warning("Back trace of this kill: %s" % (traceback.format_exc()))
                self.modules_manager.set_to_restart(mod)
                
        logger.debug("[%s] No user preferences found, returning default value: %s", self.name, default)
        return default


    # Try to got for an element the graphs uris from modules
    def set_user_preference(self, user, key, value):
        logger.debug("[%s] Saving user preference for: %s / %s", self.name, user.get_name(), key)

        for mod in self.modules_manager.get_internal_instances():
            try:
                f = getattr(mod, 'set_ui_user_preference', None)
                if f and callable(f):
                    f(user, key, value)
                    logger.debug("[%s] Updated '%s', %s = %s", self.name, user.get_name(), key, value)
            except Exception, exp:
                logger.warning("[%s] The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                logger.warning("[%s] Exception type: %s" % (self.name, type(exp)))
                logger.warning("Back trace of this kill: %s" % (traceback.format_exc()))
                self.modules_manager.set_to_restart(mod)
                
    # Get all user's common preferences ...
    def get_common_preference(self, key, default=None):
        logger.debug("[%s] Fetching common preference for: %s", self.name, key)

        for mod in self.modules_manager.get_internal_instances():
            try:
                logger.debug("[%s] Trying to get common preference %s from %s", self.name, key, mod.get_name())
                f = getattr(mod, 'get_ui_common_preference', None)
                if f and callable(f):
                    r = f(key)
                    logger.debug("[%s] Found 'common', %s = %s", self.name, key, r)
                    if r is not None:
                        return r
            except Exception, exp:
                logger.warning("[%s] The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                logger.warning("[%s] Exception type: %s" % (self.name, type(exp)))
                logger.warning("Back trace of this kill: %s" % (traceback.format_exc()))
                self.modules_manager.set_to_restart(mod)
                
        logger.debug("[%s] No common preferences found, returning default value: %s", self.name, default)
        return default


    # Set all user's common preferences ...
    def set_common_preference(self, key, value):
        logger.debug("[%s] Saving common preference: %s = %s", self.name, key, value)

        for mod in self.modules_manager.get_internal_instances():
            try:
                f = getattr(mod, 'set_ui_common_preference', None)
                if f and callable(f):
                    f(key, value)
                    logger.debug("[%s] Updated 'common', %s = %s", self.name, key, value)
            except Exception, exp:
                logger.warning("[%s] The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                logger.warning("[%s] Exception type: %s" % (self.name, type(exp)))
                logger.warning("Back trace of this kill: %s" % (traceback.format_exc()))
                self.modules_manager.set_to_restart(mod)


    # For a specific place like dashboard we return widget lists
    def get_widgets_for(self, place):
        return self.widgets.get(place, [])


    # Will get all label/uri for external UI like PNP or NagVis
    def get_ui_external_links(self):
        lst = []
        for mod in self.modules_manager.get_internal_instances():
            try:
                f = getattr(mod, 'get_external_ui_link', None)
                if f and callable(f):
                    r = f()
                    lst.append(r)
            except Exception, exp:
                logger.warning("[%s] Warning: The mod %s raise an exception: %s, I'm tagging it to restart later" % (self.name, mod.get_name(), str(exp)))
                logger.debug("[%s] Exception type: %s" % (self.name, type(exp)))
                logger.debug("Back trace of this kill: %s" % (traceback.format_exc()))
                self.modules_manager.set_to_restart(mod)

        safe_print("Will return external_ui_link::", lst)
        return lst


    def insert_template(self, tpl_name, d):
        try:
            r = template(tpl_name, d)
        except Exception, exp:
            pass#print "Exception?", exp


    def can_see_this_elt(self, elt):
        user = self.get_user_auth()
        if user.is_admin:
            return True
            
        if user in elt.contacts:
            return True
            
        return False
            
        # if elt in only_related_to(app.datamgr.get_hosts(),user):
            # return True
        # else:
            # return False
        # user = self.get_user_auth()
        # elt_cg = getattr(elt, 'contact_groups')
        # cg_users = []

        # Compile a users list with all contacts in these contactgroups
        # for cg in elt_cg:
            # cg_users = cg_users + self.datamgr.get_contactgroup(cg).get_contacts()

        # if (self.manage_acl and user in cg_users) or user.is_admin:
            # return True
        # return False 


    # Those functions should be located in Shinken core DataManager class ... should be useful for other modules than WebUI ?
    # -----------------------------------------------------------------------------------------------------------------------
    def get_hosts(self, user=None):
        items=self.datamgr.get_hosts()
        r = set()
        for h in items:
            filtered = False
            for filter in self.hosts_filter:
                if h.host_name.startswith(filter):
                    filtered = True
            if not filtered:
                    r.add(h)
                    
        if user is not None:
            r=only_related_to(r,user)

        return r
                  
    def get_services(self, user=None):
        items=self.datamgr.get_services()
        if user is not None:
            return only_related_to(items,user)

        return items

    def get_host(self, hname):
        hname = hname.decode('utf8', 'ignore')
        return self.rg.hosts.find_by_name(hname)

    def get_service(self, hname, sdesc):
        hname = hname.decode('utf8', 'ignore')
        sdesc = sdesc.decode('utf8', 'ignore')
        return self.rg.services.find_srv_by_name_and_hostname(hname, sdesc)

    def get_all_hosts_and_services(self, user=None, get_impacts=True):
        all = []
        if get_impacts:
            all.extend(self.get_hosts())
            all.extend(self.get_services())
        else:
            all.extend([h for h in self.get_hosts() if not h.is_impact])
            all.extend([s for s in self.get_services() if not s.is_impact])
        return all

    def get_timeperiods(self):
        return self.datamgr.rg.timeperiods
                  
    def get_timeperiod(self, name):
        return self.datamgr.rg.timeperiods.find_by_name(name)
    
    def get_commands(self):
        return self.datamgr.rg.commands
                  
    def get_command(self, name):
        name = name.decode('utf8', 'ignore')
        return self.datamgr.rg.commands.find_by_name(name)

    def get_contacts(self):
        return self.datamgr.rg.contacts
                  
    def get_contact(self, name):
        name = name.decode('utf8', 'ignore')
        return self.datamgr.rg.contacts.find_by_name(name)

    def get_contactgroups(self):
        # return self.datamgr.get_contactgroups()
        return self.datamgr.rg.contactgroups
                  
    def get_contactgroup(self, name):
        name = name.decode('utf8', 'ignore')
        return self.datamgr.rg.contactgroups.find_by_name(name)

    def set_hostgroups_level(self, user=None):
        logger.debug("[%s] set_hostgroups_level", self.name)
        
        # All known hostgroups are level 0 groups ...
        for group in self.get_hostgroups(user=user):
            self.set_hostgroup_level(group, 0, user)
        
    def set_hostgroup_level(self, group, level, user=None):
        logger.debug("[%s] set_hostgroup_level, group: %s, level: %d", self.name, group.hostgroup_name, level)
        
        setattr(group, 'level', level)
                
        # Search hostgroups referenced in another group
        if group.has('hostgroup_members'):
            for g in sorted(group.get_hostgroup_members()):
                logger.debug("[%s] set_hostgroups_level, group: %s, level: %d", self.name, g, group.level + 1)
                child_group = self.get_hostgroup(g)
                self.set_hostgroup_level(child_group, level + 1, user)
        
    def get_hostgroups(self, user=None):
        items=self.datamgr.rg.hostgroups
        
        r = set()
        for g in items:
            filtered = False
            for filter in self.hosts_filter:
                if g.hostgroup_name.startswith(filter):
                    filtered = True
            if not filtered:
                    r.add(g)
                    
        if user is not None:
            r=only_related_to(r,user)

        return r

    def get_hostgroup(self, name):
        return self.datamgr.rg.hostgroups.find_by_name(name)
                  
    def set_servicegroups_level(self, user=None):
        logger.debug("[%s] set_servicegroups_level", self.name)
        
        # All known hostgroups are level 0 groups ...
        for group in self.get_servicegroups(user=user):
            self.set_servicegroup_level(group, 0, user)
        
    def set_servicegroup_level(self, group, level, user=None):
        logger.debug("[%s] set_servicegroup_level, group: %s, level: %d", self.name, group.servicegroup_name, level)
        
        setattr(group, 'level', level)
                
        # Search hostgroups referenced in another group
        if group.has('servicegroup_members'):
            for g in sorted(group.get_servicegroup_members()):
                logger.debug("[%s] set_servicegroups_level, group: %s, level: %d", self.name, g, group.level + 1)
                child_group = self.get_servicegroup(g)
                self.set_servicegroup_level(child_group, level + 1, user)
        
    def get_servicegroups(self, user=None):
        items = self.datamgr.rg.servicegroups
        
        r = set()
        for g in items:
            filtered = False
            for filter in self.services_filter:
                if g.servicegroup_name.startswith(filter):
                    filtered = True
            if not filtered:
                    r.add(g)
                    
        if user is not None:
            r=only_related_to(r,user)

        return r

    def get_servicegroup(self, name):
        return self.datamgr.rg.servicegroups.find_by_name(name)
                  
    # Get the hosts tags sorted by names, and zero size in the end
    def get_host_tags_sorted(self):
        r = []
        names = self.datamgr.rg.tags.keys()
        names.sort()
        for n in names:
            r.append((n, self.datamgr.rg.tags[n]))
        return r

    # Get the hosts tagged with a specific tag
    def get_hosts_tagged_with(self, tag):
        r = []
        for h in self.get_hosts():
            if tag in h.get_host_tags():
                r.append(h)
        return r

    # Get the services tags sorted by names, and zero size in the end
    def get_service_tags_sorted(self):
        r = []
        names = self.datamgr.rg.services_tags.keys()
        names.sort()
        for n in names:
            r.append((n, self.datamgr.rg.services_tags[n]))
        return r

    # Get the services tagged with a specific tag
    def get_services_tagged_with(self, tag):
        r = []
        for s in self.get_services():
            if tag in s.get_service_tags():
                r.append(s)
        return r

    # Returns all problems
    # Not really useful ... to be confirmed !
    def get_all_problems(self, user=None, to_sort=True, get_acknowledged=False):
        res = []
        if not get_acknowledged:
            res.extend([s for s in self.get_services(user) if s.state not in ['OK', 'PENDING'] and not s.is_impact and not s.problem_has_been_acknowledged and not s.host.problem_has_been_acknowledged])
            res.extend([h for h in self.get_hosts(user) if h.state not in ['UP', 'PENDING'] and not h.is_impact and not h.problem_has_been_acknowledged])
        else:
            res.extend([s for s in self.get_services(user) if s.state not in ['OK', 'PENDING'] and not s.is_impact])
            res.extend([h for h in self.get_hosts(user) if h.state not in ['UP', 'PENDING'] and not h.is_impact])

        if to_sort:
            res.sort(hst_srv_sort)
        return res

    # Return the number of problems
    def get_nb_problems(self, user=None, to_sort=True, get_acknowledged=False):
        return len(self.get_all_problems(user, to_sort, get_acknowledged))
        
    # For all business impacting elements, and give the worse state
    # if warning or critical
    def get_overall_state(self, user=None):
        h_states = [h.state_id for h in self.get_hosts(user) if h.business_impact > 2 and h.is_impact and h.state_id in [1, 2]]
        s_states = [s.state_id for s in self.get_services(user) if s.business_impact > 2 and s.is_impact and s.state_id in [1, 2]]
        if len(h_states) == 0:
            h_state = 0
        else:
            h_state = max(h_states)
        if len(s_states) == 0:
            s_state = 0
        else:
            s_state = max(s_states)

        return max(h_state, s_state)

     # For all business impacting elements, and give the worse state
    # if warning or critical
    def get_overall_state_problems_count(self, user=None):
        h_states = [h.state_id for h in self.get_hosts(user) if h.business_impact > 2 and h.is_impact and h.state_id in [1, 2]]
        logger.debug("[%s] get_overall_state_problems_count, hosts: %d", self.name, len(h_states))
        s_states = [s.state_id for s in self.get_services(user) if  s.business_impact > 2 and s.is_impact and s.state_id in [1, 2]]
        logger.debug("[%s] get_overall_state_problems_count, hosts+services: %d", self.name, len(s_states))
        
        return len(h_states) + len(s_states)

   # Same but for pure IT problems
    def get_overall_it_state(self, user=None):
        h_states = [h.state_id for h in self.get_hosts(user) if h.is_problem and h.state_id in [1, 2]]
        s_states = [s.state_id for s in self.get_services(user) if s.is_problem and s.state_id in [1, 2]]
        if len(h_states) == 0:
            h_state = 0
        else:
            h_state = max(h_states)
        if len(s_states) == 0:
            s_state = 0
        else:
            s_state = max(s_states)
            
        return max(h_state, s_state)

    # Get the number of all problems, even the ack ones
    def get_overall_it_problems_count(self, user=None, get_acknowledged=False):
        logger.debug("[%s] get_overall_it_problems_count, user: %s, get_acknowledged: %d", self.name, user.contact_name, get_acknowledged)
        
        if not get_acknowledged:
            h_states = [h for h in self.get_hosts(user) if h.state not in ['UP', 'PENDING'] and not h.is_impact and not h.problem_has_been_acknowledged]
            s_states = [s for s in self.get_services(user) if s.state not in ['OK', 'PENDING'] and not s.is_impact and not s.problem_has_been_acknowledged and not s.host.problem_has_been_acknowledged]
        else:
            h_states = [h for h in self.get_hosts(user) if h.state not in ['UP', 'PENDING'] and not h.is_impact]
            s_states = [s for s in self.get_services(user) if s.state not in ['OK', 'PENDING'] and not s.is_impact]
            
        logger.debug("[%s] get_overall_it_problems_count, hosts: %d", self.name, len(h_states))
        logger.debug("[%s] get_overall_it_problems_count, services: %d", self.name, len(s_states))
        
        return len(h_states) + len(s_states)

    # Get percentage of all Services
    def get_percentage_service_state(self, user=None):
        all_services = self.get_services(user)
        problem_services = []
        problem_services.extend([s for s in all_services if s.state not in ['OK', 'PENDING'] and not s.is_impact])
        if len(all_services) == 0:
            res = 0
        else:
            res = int(100-(len(problem_services) *100)/float(len(all_services)))
        return res
              
    # Get percentage of all Hosts
    def get_percentage_hosts_state(self, user=None):
        all_hosts = self.get_hosts(user)
        problem_hosts = []
        problem_hosts.extend([s for s in all_hosts if s.state not in ['UP', 'PENDING'] and not s.is_impact])
        if len(all_hosts) == 0:
            res = 0
        else:
            res = int(100-(len(problem_hosts) *100)/float(len(all_hosts)))
        return res
