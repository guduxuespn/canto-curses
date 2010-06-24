# -*- coding: utf-8 -*-
#Canto-curses - ncurses RSS reader
#   Copyright (C) 2010 Jack Miller <jack@codezen.org>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as 
#   published by the Free Software Foundation.

from canto.client import CantoClient
from canto.encoding import decoder
from gui import CantoCursesGui

from threading import Thread, Lock

import logging

logging.basicConfig(
        filemode = "w",
        format = "%(asctime)s : %(name)s -> %(message)s",
        datefmt = "%H:%M:%S",
        level = logging.DEBUG
)

log = logging.getLogger("CANTO-CURSES")

import traceback
import locale
import getopt
import errno
import fcntl
import time
import sys
import os

class CantoCurses(CantoClient):

    # Init separate from instantiation for test purposes.
    def __init__(self):
        pass

    def init(self, args=None, do_log=True):

        # For good curses behavior.
        locale.setlocale(locale.LC_ALL, '')

        if self.args(args):
            sys.exit(-1)

        self.start_daemon()

        # The daemon is backed, init our base class,
        # start trying to connect to the daemon.

        try:
            CantoClient.__init__(self, self.socket_path)
        except Exception, e:
            log.error("Error: %s" % e)
            sys.exit(-1)

        if self.ensure_files():
            sys.exit(-1)

        if do_log:
            self.set_log()

        self.response_lock = Lock()

    def response_thread(self):
        try:
            while self.response_alive:
                r = self.read(1)

                # HUP
                if r == 16:
                    break
                if r:
                    self.response_lock.acquire()
                    self.responses.append(r)
                    self.response_lock.release()
        except Exception, e:
            log.error("Response thread exception: %s" % (e,))

        log.debug("Response thread exiting.")

    def start_thread(self):
        self.response_alive = True
        self.responses = []

        # Thead *must* be running before gui instantiated
        # so the __init__ can ram some discovery requests through.
        thread = Thread(target=self.response_thread)
        thread.start()

    def run(self):
        self.start_thread()

        self.gui = CantoCursesGui()
        self.gui.init(self)

        while True:
            self.gui.run()

    def args(self, args):
        if not args:
            args = sys.argv[1:]

        try:
            optlist = getopt.getopt(args, 'D:', ["dir="])[0]
        except getopt.GetoptError, e:
            log.error("Error: %s" % e.msg)

        self.conf_dir = os.path.expanduser(u"~/.canto-ng/")

        for opt, arg in optlist:
            if opt in [ "-D", "--dir"]:
                self.conf_dir = os.path.expanduser(decoder(arg))
                self.conf_dir = os.path.realpath(self.conf_dir)

        self.socket_path = self.conf_dir + "/.canto_socket"

        return 0

    def start_daemon(self):
        pid = os.fork()
        if not pid:
            # Shutup any log output before canto-daemon
            # sets up it's log (particularly the error that
            # one is already running)

            fd = os.open("/dev/null", os.O_RDWR)
            os.dup2(fd, sys.stderr.fileno())

            os.setpgid(os.getpid(), os.getpid())
            os.execve("/bin/sh",
                     ["/bin/sh", "-c", "canto-daemon -D " + self.conf_dir],
                     os.environ)

            # Should never get here, but just in case.
            sys.exit(-1)

        while not os.path.exists(self.socket_path):
            time.sleep(0.1)

        return pid

    def ensure_files(self):
        for f in [ "curses-log" ] :
            p = self.conf_dir + "/" + f
            if os.path.exists(p):
                if not os.path.isfile(p):
                    log.error("Error: %s is not a file." % p)
                    return -1
                if not os.access(p, os.R_OK):
                    log.error("Error: %s is not readable." % p)
                    return -1
                if not os.access(p, os.W_OK):
                    log.error("Error: %s is not writable." % p)
                    return -1

        self.log_path = self.conf_dir + "/curses-log"

    def set_log(self):
        f = open(self.log_path, "w")
        os.dup2(f.fileno(), sys.stderr.fileno())

    def start(self, args=None):
        try:
            self.init(args)
            self.run()
        except KeyboardInterrupt:
            pass

        except Exception, e:
            tb = traceback.format_exc(e)
            log.error("Exiting on exception:")
            log.error("\n" + "".join(tb))

        sys.exit(0)
