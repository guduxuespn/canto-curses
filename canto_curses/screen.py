# -*- coding: utf-8 -*-
#Canto-curses - ncurses RSS reader
#   Copyright (C) 2010 Jack Miller <jack@codezen.org>
#
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License version 2 as 
#   published by the Free Software Foundation.

from canto_next.plugins import Plugin
from canto_next.encoding import locale_enc
from canto_next.hooks import on_hook

from .command import CommandHandler, cmd_complete
from .taglist import TagList
from .input import InputBox
from .text import InfoBox
from .widecurse import wsize, set_redisplay_callback, set_getc
from .locks import sync_lock

from threading import Lock
import traceback
import readline
import logging
import curses
import time
import os

log = logging.getLogger("SCREEN")

# The Screen class handles the layout of multiple sub-windows on the main
# curses window. It's also the top-level gui object, so it handles calls to
# refresh the screen, get input, and curses related console commands, like
# "color".

# There are two types of windows that the Screen class handles. The first are
# normal windows (in self.tiles). These windows are all tiled in a single
# layout (determined by self.layout and self.fill_layout()) and rendered first.

# The other types are floats that are rendered on top of the window layout.
# These floats are all independent of each other.

# The Screen class is also in charge of honoring the window specific
# configuration options. Like window.{maxwidth,maxheight,float}.

class ScreenPlugin(Plugin):
    pass

class Screen(CommandHandler):
    def __init__(self, callbacks, types = [InputBox, TagList]):
        CommandHandler.__init__(self)

        self.plugin_class = ScreenPlugin
        self.update_plugin_lookups()

        self.callbacks = callbacks
        self.layout = "default"

        self.window_types = types

        self.stdscr = curses.initscr()
        if self.curses_setup() < 0:
            return -1

        self.pseudo_input_box = curses.newpad(1,1)

        self.pseudo_input_box.keypad(1)
        self.pseudo_input_box.nodelay(0)
        self.input_lock = Lock()

        readline.set_completion_display_matches_hook(self.readline_display_matches)
        set_redisplay_callback(self.readline_redisplay)
        set_getc(self.readline_getc)

        # See Python bug 2675, readline + curses
        os.unsetenv('LINES')
        os.unsetenv('COLUMNS')

        for line in ['tab: complete',\
                'set show-all-if-ambiguous on']:
            readline.parse_and_bind(line)

        readline.set_completer(self.readline_complete)

        self.floats = []
        self.tiles = []
        self.windows = []

        self.subwindows()

        # Start grabbing user input
        #self.start_input_thread()

        on_hook("curses_opt_change", self.screen_opt_change)

    # Wrap curses.curs_set in exception handler
    # because we don't really care if it's displayed
    # on terminals that don't like it.

    def curs_set(self, n):
        try:
            curses.curs_set(n)
        except:
            pass

    # Do initial curses setup. This should only be done on init, or after
    # endwin() (i.e. resize).

    def curses_setup(self):
        self.curs_set(0)

        try:
            curses.cbreak()
            curses.noecho()
            curses.start_color()
            curses.use_default_colors()
        except Exception as e:
            log.error("Curses setup failed: %s" % e.msg)
            return -1

        self.height, self.width = self.stdscr.getmaxyx()
        self.height = int(self.height)
        self.width = int(self.width)

        color_conf = self.callbacks["get_opt"]("color")

        for i in range(curses.COLOR_PAIRS):
            if ("%s" % i) not in color_conf:
                continue

            color = color_conf["%s" % i]

            if type(color) == int:
                fg = color
                bg = color_conf['defbg']
            else:
                if 'fg' in color:
                    fg = color['fg']
                else:
                    fg = color_conf['deffg']

                if 'bg' in color:
                    bg = color['bg']
                else:
                    bg = color_conf['defbg']

            try:
                curses.init_pair(i + 1, fg, bg)
            except:
                log.error("color pair failed!: %d fg: %d bg: %d" %
                        (i + 1, fg, bg))
        return 0

    def screen_opt_change(self, conf):
        # Require resize even to re-init curses and colors.
        if "color" in conf:
            self.callbacks["set_var"]("needs_resize", True)

        for key in list(conf.keys()):
            if type(conf[key]) == dict and "window" in conf[key]:
                self.callbacks["set_var"]("needs_resize", True)
                break

    # _subw_size functions enforce the height and width of windows.
    # It returns the minimum of:
    #       - The maximum size (given by layout)
    #       - The requested size (given by the class)
    #       - The configured size (given by the config)

    def _subw_size_height(self, ci, height):
        window_conf = self.callbacks["get_opt"](ci.get_opt_name() + ".window")

        if not window_conf["maxheight"]:
            window_conf["maxheight"] = height
        req_height = ci.get_height(height)

        return min(height, window_conf["maxheight"], req_height)

    def _subw_size_width(self, ci, width):
        window_conf = self.callbacks["get_opt"](ci.get_opt_name() + ".window")

        if not window_conf["maxwidth"]:
            window_conf["maxwidth"] = width
        req_width = ci.get_width(width)

        return min(width, window_conf["maxwidth"], req_width)

    # _subw_layout_size will return the total size of layout
    # in either height or width where layout is a list of curses
    # pads, or sublists of curses pads.

    def _subw_layout_size(self, layout, dim):

        # Grab index into pad.getmaxyx()
        if dim == "width":
            idx = 1
        elif dim == "height":
            idx = 0
        else:
            raise Exception("Unknown dim: %s" % dim)

        sizes = []
        for x in layout:
            if hasattr(x, "__iter__"):
                sizes.append(self._subw_layout_size(x, dim))
            else:
                sizes.append(x.pad.getmaxyx()[idx] - 1)

        return max(sizes)

    # Translate the layout into a set of curses pads given
    # a set of coordinates relating to how they're mapped to the screen.

    def _subw_init(self, ci, top, left, height, width):

        # Height - 1 because start + height = line after bottom.

        bottom = top + (height - 1)
        right = left + (width - 1)

        # lambda this up so that subwindows truly have no idea where on the
        # screen they are, only their dimensions, but can still selectively
        # refresh their portion of the screen.

        refcb = lambda : self.refresh_callback(ci, top, left, bottom, right)

        # Callback to allow windows to know if they're floating. This is
        # important because floating windows are only rendered up to their
        # last cursor position, despite being given a maximal window.

        floatcb = lambda : ci in self.floats

        # Use coordinates and dimensions to determine where borders
        # are needed. This is independent of whether there are actually
        # windows there.

        # NOTE: These should only be honored if the window is non-floating.
        # Floating windows are, by design, given a window the size of the
        # entire screen, but only actually written lines are drawn.

        window_conf = self.callbacks["get_opt"](ci.get_opt_name() + ".window")

        if window_conf['border'] == "smart":
            top_border = top != 0
            bottom_border = bottom != (self.height - 1)
            left_border = left != 0
            right_border = right != (self.width - 1)

            if ci in self.floats:
                if "top" in window_conf['align']:
                    bottom_border = True
                if "bottom" in window_conf['align']:
                    top_border = True

        elif window_conf['border'] == "full":
            top_border, bottom_border, left_border, right_border = (True,) * 4

        elif window_conf['border'] == "none":
            top_border, bottom_border, left_border, right_border = (False,) * 4

        bordercb = lambda : (top_border, left_border, bottom_border, right_border)

        # Height + 1 to account for the last curses pad line
        # not being fully writable.

        log.debug("h: %s w: %s" % (self.height, self.width))
        log.debug("h: %s w: %s" % (height, width))
        pad = curses.newpad(height + 1, width)

        # Pass on callbacks we were given from CantoCursesGui
        # plus our own.

        callbacks = self.callbacks.copy()
        callbacks["refresh"] = refcb
        callbacks["border"] = bordercb
        callbacks["floating"] = floatcb
        callbacks["input"] = self.input_callback
        callbacks["die"] = self.die_callback
        callbacks["pause_interface" ] = self.pause_interface_callback
        callbacks["unpause_interface"] = self.unpause_interface_callback
        callbacks["add_window"] = self.add_window_callback

        ci.init(pad, callbacks)

    # Layout some windows into the given space, stacking with
    # orientation horizontally or vertically.

    def _subw(self, layout, top, left, height, width, orientation):
        immediates = []
        cmplx = []
        sizes = [0] * len(layout)

        # Separate windows in to two categories:
        # immediates that are defined as base classes and
        # cmplx which are lists for further processing (iterables)

        for i, unit in enumerate(layout):
            if hasattr(unit, "__iter__"):
                cmplx.append((i, unit))
            else:
                immediates.append((i,unit))

        # Units are the number of windows we'll have
        # to split the area with.

        units = len(layout)

        # Used, the amounts of space already used.
        used = 0

        for i, unit in immediates:
            # Get the size of the window from the class.
            # Each class is given, as a maximum, the largest
            # possible slice we can *guarantee*.

            if orientation == "horizontal":
                size = self._subw_size_width(unit, int((width - used) / units))
            else:
                size = self._subw_size_height(unit, int((height - used) / units))

            used += size

            sizes[i] = size

            # Subtract so that the next run only divides
            # the remaining space by the number of units
            # that don't have space allocated.

            units -= 1

        # All of the immediates have been allocated for.
        # So now only the cmplxs are vying for space.

        units = len(cmplx)

        for i, unit in cmplx:
            offset = sum(sizes[0:i])

            # Recursives call this function, alternating
            # the orientation, for the space we can guarantee
            # this set of windows.

            if orientation == "horizontal":
                available = int((width - used) / units)
                r = self._subw(unit, top, left + offset,\
                        height, available, "vertical")
                sizes[i] = self._subw_layout_size(r, "width")
            else:
                available = int((height - used) / units)
                r = self._subw(unit, top + offset, left,\
                        available, width, "horizontal")
                sizes[i] = self._subw_layout_size(r, "height")

            used += sizes[i]
            units -= 1

        # Now that we know the actual sizes (and thus locations) of
        # the windows, we actually setup the immediates.

        for i, ci in immediates:
            offset = sum(sizes[0:i])
            if orientation == "horizontal":
                self._subw_init(ci, top, left + offset,
                        height, sizes[i])
            else:
                self._subw_init(ci, top + offset, left,
                        sizes[i], width)
        return layout

    # The fill_layout() function takes a list of active windows and generates a
    # list based layout. The depth of a window in the list determines its
    # orientation.
    #
    #   Example return: [ Window1, Window2 ]
    #       - Window1 on top of Window2, each taking half of the vertical space.
    #
    #   Example return: [ [ Window1, Window2 ], Window 3 ]
    #       - Window1 left of Window2 each taking half of the horizontal space,
    #           and whatever vertical space left by Window3, because Window3 is
    #           shallower than 1 or 2, so it's size is evaluated first and the
    #           remaining given to the [ Window1, Window2 ] horizontal layout.
    #
    #   Example return: [ [ [ [ Window1 ] ], Window2 ], Window3 ]
    #       - Same as above, except because Window1 is deeper than Window2 now,
    #           Window2's size is evaluated first and Window1 is given all of 
    #           the remaining space.
    #
    #   NOTE: Floating windows are not handled in the layout, this is solely for
    #   the tiling bottom layer of windows.

    def fill_layout(self, layout, windows):
        inputs = [ w for w in windows if w.is_input() ]
        if inputs:
            self.input_box = inputs[0]
        else:
            self.input_box = None

        # Simple stacking, even distribution between all windows.
        if layout == "hstack":
            return windows
        elif layout == "vstack":
            return [ windows ]
        else:
            aligns = { "top" : [], "bottom" : [], "left" : [], "right" : [],
                            "neutral" : [] }

            # Separate windows by alignment.
            for w in windows:
                align = self.callbacks["get_opt"]\
                        (w.get_opt_name() + ".window.align")

                # Move taglist deeper so that it absorbs any
                # extra space left in the rest of the layout.

                if w.get_opt_name() == "taglist":
                    aligns[align].append([[w]])
                else:
                    aligns[align].append(w)

            horizontal = aligns["left"] + aligns["neutral"] + aligns["right"]
            return aligns["top"] + [horizontal] + aligns["bottom"]

    # subwindows() is the top level window generator. It handles both the bottom
    # level tiled window layout as well as the floats.

    def subwindows(self):

        # Cleanup any window objects that will be destroyed.
        for w in self.windows:
            w.die()

        self.floats = []
        self.tiles = []
        self.windows = []

        # Instantiate new windows, separating them into
        # floating and tiling windows.

        for wt in self.window_types:
            w = wt()
            optname = w.get_opt_name()
            flt = self.callbacks["get_opt"](optname + ".window.float")
            if flt:
                self.floats.append(w)
            else:
                self.tiles.append(w)
            self.windows.append(w)

        # Focused window will no longer exist.
        self.focused = None

        # Init tiled windows.
        l = self.fill_layout(self.layout, self.tiles)
        self._subw(l, 0, 0, self.height, self.width, "vertical")

        # Init floating windows.
        for f in self.floats: 
            align = self.callbacks["get_opt"]\
                    (f.get_opt_name() + ".window.align")
            height = self._subw_size_height(f, self.height)
            width = self._subw_size_width(f, self.width)

            top = 0
            if align.startswith("bottom"):
                top = self.height - height

            left = 0
            if align.endswith("right"):
                left = self.width - width

            self._subw_init(f, top, left, height, width)

        # Default to giving first window focus.
        self._focus_abs(0)

    def refresh_callback(self, c, t, l, b, r):
        if c in self.floats:
            b = min(b, t + c.pad.getyx()[0])
        c.pad.noutrefresh(0, 0, t, l, b, r)

    def input_callback(self, prompt):
        # Setup subedit
        self.curs_set(1)

        self.callbacks["set_var"]("input_prompt", prompt)
        self.input_box.reset()
        self.input_box.refresh()
        curses.doupdate()

        r = input()
        readline.add_history(r)

        self.callbacks["set_var"]("input_prompt", "")
        self.input_box.reset()
        self.input_box.refresh()
        curses.doupdate()

        self.curs_set(0)
        return r

    def die_callback(self, window):
        # Call the window's die function
        window.die()

        # Remove window from both window_types and the general window list
        idx = self.windows.index(window)
        del self.windows[idx]
        del self.window_types[idx]

        # Regenerate layout with remaining windows.
        self.subwindows()

        self.refresh()

        # Force a doupdate because refresh doesn't, but we have possibly
        # uncovered part of the screen that isn't handled by any other window.

        curses.doupdate()

    # The pause interface callback keeps the interface from updating. This is
    # useful if we have to temporarily surrender the screen (i.e. text browser).

    # NOTE: This does not affect signals so even while "paused", c-c continues
    # to take things like SIGWINCH which will be interpreted on wakeup.

    # NOTE: This callback must be called from within the GUI thread, and the
    # calling function must call unpause *without* returning.

    def pause_interface_callback(self):
        log.debug("Pausing interface.")
        self.input_lock.acquire()

    def unpause_interface_callback(self):
        log.debug("Unpausing interface.")
        self.input_lock.release()

        # All of our window information could be stale.
        self._resize()

    def add_window_callback(self, cls):
        self.window_types.append(cls)

        self.subwindows()

        # Focus new window
        self._focus_abs(0)

        self.refresh()
        self.redraw()

    def _readline_redisplay(self):
        log.debug("rredisplay: %s" % readline.get_line_buffer())
        self.input_box.set_content(readline.get_line_buffer())
        self.input_box.refresh()
        curses.doupdate()

    def _readline_complete(self, prefix, index):
        log.debug("rcomplete: %s %s" % (prefix, index))
        r = cmd_complete(prefix, index)
        log.debug("rcomp ret: %s" % (r,))
        return r

    def _readline_display_matches(self, sub, matches, maxlen):
        log.debug("rdispmatch: %s - %s - %s" % (sub, matches, maxlen))
        self.callbacks["set_var"]("info_msg", "Matches: %s\n" % '\n'.join(matches))

        self.input_box.rotate_completions(sub, matches)

        # We're called from readline, so we have to take sync_lock before we
        # cause anything other than the input box to refresh/redraw

        sync_lock.acquire_write()
        self.refresh()
        self.redraw()
        sync_lock.release_write()

    def _readline_getc(self):
        r = self.get_key()

        # Reject current completion
        if chr(r) == "\b":
            self.input_box.break_completion()

        # Accept current completion
        elif chr(r) == " ":
            comp = self.input_box.break_completion()
            if comp:
                readline.insert_text(comp)

        return r

    def _exception_wrap(self, fn, *args):
        r = None
        try:
            r = fn(*args)
        except:
            log.error("".join(traceback.format_exc()))
        return r

    def readline_redisplay(self, *args):
        return self._exception_wrap(self._readline_redisplay, *args)
    def readline_complete(self, *args):
        return self._exception_wrap(self._readline_complete, *args)
    def readline_display_matches(self, *args):
        return self._exception_wrap(self._readline_display_matches, *args)
    def readline_getc(self, *args):
        return self._exception_wrap(self._readline_getc, *args)

    # Refresh operates in order, which doesn't matter for top level tiled
    # windows, but this ensures that floats are ordered such that the last
    # floating window is rendered on top of all others.

    def refresh(self):
        for c in self.tiles + self.floats:
            c.refresh()

    def redraw(self):
        for c in self.tiles + self.floats:
            c.redraw()
        curses.doupdate()

    def cmd_resize(self, **kwargs):
        self.resize()

    # Typical curses resize, endwin and re-setup.
    def resize(self):
        try:
            curses.endwin()
        except:
            pass

        self.pseudo_input_box.keypad(1)
        self.pseudo_input_box.nodelay(0)
        self.stdscr.refresh()

        self.curses_setup()
        self.subwindows()
        self.refresh()
        self.redraw()

    # Focus idx-th window.
    def cmd_focus(self, **kwargs):
        self._focus_abs(kwargs["idx"])

    def _focus_abs(self, idx):
        focus_order = self.tiles + self.floats
        focus_order.reverse()
        l = len(focus_order)

        if idx < 0:
            idx = -1 * (idx % l)
        else:
            idx %= l

        self._focus(focus_order[idx])

    def cmd_focus_rel(self, **kwargs):
        focus_order = [w for w in self.tiles + self.floats if not w.is_input()]
        log.debug("focus_order: %s" % focus_order)
        focus_order.reverse()

        idx = focus_order.index(self.focused) + kwargs["idx"]
        l = len(focus_order)

        if idx < 0:
            idx = -1 * (idx % l)
        else:
            idx %= l

        self._focus(focus_order[idx])

    def _focus(self, win):
        self.focused = win
        log.debug("Focusing window (%s)" % (self.focused,))

    # Dump all top-level curses windows to a file.
    # NOTE: This is intended for test use only. This
    # command does no error handling.

    def cmd_dump_screen(self, **kwargs):
        f = open(kwargs["filename"], "wb")

        for w in self.windows:
            startpos = f.tell()
            w.pad.putwin(f)
            endpos = f.tell()

            # Overwrite struct output.
            f.seek(startpos, 0)
            f.write("\0" * wsize())
            f.seek(endpos, 0)

        f.close()

    def cmd_color(self, **kwargs):
        conf = self.callbacks["get_conf"]()
        idx = kwargs["idx"]

        if type(conf["color"][idx]) == dict:
            fg = conf["color"][idx]["fg"]
            bg = conf["color"][idx]["bg"]
        else:
            fg = conf["color"][idx]
            bg = None

        fg = kwargs["fg"]
        if kwargs["bg"] != None:
            bg = kwargs["bg"]

        # Deffg and defbg obviously only have one color.
        if idx in [ "deffg", "deffg" ] or bg == None:
            conf["color"][idx] = fg
        else:
            conf["color"][idx] = { "fg" : fg, "bg" : bg }

        log.debug("color set: %s" % conf["color"][idx])

        self.callbacks["set_conf"](conf)

    def get_focus_list(self):
        return [ self, self.focused ]

    def get_key(self):
        self.input_lock.acquire()
        try:
            r = self.pseudo_input_box.get_wch()
        except Exception as e:
            r = self.pseudo_input_box.getch()
        self.input_lock.release()
        if type(r) == str:
            r = ord(r)
        return r

    def exit(self):
        curses.endwin()

    def get_opt_name(self):
        return "screen"
