# -*- coding: utf-8 -*-
#Canto-curses - ncurses RSS reader
#   Copyright (C) 2010 Jack Miller <jack@codezen.org>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as 
#   published by the Free Software Foundation.

from canto_next.plugins import Plugin
from .guibase import GuiBase

import logging
log = logging.getLogger("INPUT")

import curses
from curses import ascii

class InputPlugin(Plugin):
    pass

class InputBox(GuiBase):
    def __init__(self):
        GuiBase.__init__(self)
        self.plugin_class = InputPlugin

    def init(self, pad, callbacks):
        self.pad = pad

        self.callbacks = callbacks

        self.keys = {}

        self.reset()

    def reset(self, prompt_str=None):
        self.pad.erase()
        if prompt_str:
            self.pad.addstr(prompt_str)
        self.minx = self.pad.getyx()[1]
        self.x = self.minx
        self.result = ""

    def refresh(self):
        self.pad.move(0, self.minx)
        maxx = self.pad.getmaxyx()[1]
        try:
            self.pad.addstr(self.result[-1 * (maxx - self.minx):])
        except:
            pass
        self.pad.clrtoeol()
        self.pad.move(0, min(self.x, maxx - 1))
        self.callbacks["refresh"]()

    def redraw(self):
        self.refresh()

    def addkey(self, ch):
        if ch in (ascii.STX, curses.KEY_LEFT):
            if self.x > self.minx:
                self.x -= 1
        elif ch in (ascii.BS, curses.KEY_BACKSPACE, ascii.DEL):
            if self.x > self.minx:
                idx = self.x - self.minx
                self.result = self.result[:idx - 1] + self.result[idx:]
                self.x -= 1
        elif ch == curses.KEY_DC: # Delete
            if self.x < self.minx + len(self.result):
                idx = self.x - self.minx
                self.result = self.result[:idx] + self.result[idx + 1:]
        elif ch in (ascii.ACK, curses.KEY_RIGHT): # C-f
            self.x += 1
            if len(self.result) + self.minx < self.x:
                self.result += " "
        elif ch in (ascii.ENQ, curses.KEY_END): # C-e
            self.x = self.minx + len(self.result)
        elif ch in (ascii.SOH, curses.KEY_HOME): # C-a
            self.x = self.minx
        elif ch == ascii.NL: # C-j
            return 0
        elif ch == ascii.BEL: # C-g
            self.result = ""
            return 0
        else:
            idx = self.x - self.minx
            self.result = self.result[:idx] + chr(ch) + self.result[idx:]
            self.x += 1

        self.refresh()
        curses.doupdate()
        return 1

    def edit(self, prompt=":"):
        # Render initial prompt
        self.reset(prompt)
        self.refresh()
        curses.doupdate()

    def is_input(self):
        return True

    def get_opt_name(self):
        return "input"

    def get_height(self, mheight):
        return 1

    def get_width(self, mwidth):
        return mwidth
