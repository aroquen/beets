# This file is part of beets.
# Copyright 2013, Fabrice Laporte.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.

import logging
import subprocess
import os
import collections
import itertools
import sys
import copy
import time

from beets import ui
from beets.plugins import BeetsPlugin
from beets.util import syspath, command_output
from beets import config

log = logging.getLogger('beets')


class ReplayGainError(Exception):
    """Raised when an error occurs during mp3gain/aacgain execution.
    """

def call(args):
    """Execute the command and return its output or raise a
    ReplayGainError on failure.
    """
    try:
        return command_output(args)
    except subprocess.CalledProcessError as e:
        raise ReplayGainError(
            "{0} exited with status {1}".format(args[0], e.returncode)
        )
    except UnicodeEncodeError:
        # Due to a bug in Python 2's subprocess on Windows, Unicode
        # filenames can fail to encode on that platform. See:
        # http://code.google.com/p/beets/issues/detail?id=499
        raise ReplayGainError("argument encoding failed")

class Backend(object):
    Gain = collections.namedtuple("Gain", "gain peak")
    AlbumGain = collections.namedtuple("AlbumGain", "album_gain track_gains")

    def __init__(self, config):
        pass

    def compute_track_gain(self, items):
        raise NotImplementedError()

    def compute_album_gain(self, album):
        # TODO: implement album gain in terms of track gain of the individual tracks which can be used for any backend.
        raise NotImplementedError()


class CommandBackend(Backend):
    def __init__(self, config):
        super(CommandBackend, self).__init__(config)
        
        self.command = config["command"].get(unicode)
        
        if self.command:
            # Explicit executable path.
            if not os.path.isfile(self.command):
                raise ui.UserError(
                    'replaygain command does not exist: {0}'.format(
                        self.command
                    )
                )
        else:
            # Check whether the program is in $PATH.
            for cmd in ('mp3gain', 'aacgain'):
                try:
                    call([cmd, '-v'])
                    self.command = cmd
                except OSError:
                    pass
        if not self.command:
            raise ui.UserError(
                'no replaygain command found: install mp3gain or aacgain'
            )
        
        self.noclip = config['noclip'].get(bool)
        target_level = config['targetlevel'].as_number()
        self.gain_offset = int(target_level - 89)

    def compute_track_gain(self, items):
        supported_items = filter(self.format_supported, items)
        output = self.compute_gain(supported_items, False)
        return output

    def compute_album_gain(self, album):
        # TODO: What should be done when not all tracks in the album are supported?

        supported_items = filter(self.format_supported, album.items())
        if len(supported_items) != len(album.items()):
            return Backend.AlbumGain(None, [])

        output = self.compute_gain(supported_items, True)
        return Backend.AlbumGain(output[-1], output[:-1])
    
    def format_supported(self, item):
        if 'mp3gain' in self.command and item.format != 'MP3':
            return False
        elif 'aacgain' in self.command and item.format not in ('MP3', 'AAC'):
            return False
        return True

    def compute_gain(self, items, is_album):
        if len(items) == 0:
            return []

        """Compute ReplayGain values and return a list of results
        dictionaries as given by `parse_tool_output`.
        """
        # Construct shell command. The "-o" option makes the output
        # easily parseable (tab-delimited). "-s s" forces gain
        # recalculation even if tags are already present and disables
        # tag-writing; this turns the mp3gain/aacgain tool into a gain
        # calculator rather than a tag manipulator because we take care
        # of changing tags ourselves.
        cmd = [self.command, '-o', '-s', 's']
        if self.noclip:
            # Adjust to avoid clipping.
            cmd = cmd + ['-k']
        else:
            # Disable clipping warning.
            cmd = cmd + ['-c']
        cmd = cmd + ['-a' if is_album else '-r']
        cmd = cmd + ['-d', str(self.gain_offset)]
        cmd = cmd + [syspath(i.path) for i in items]

        log.debug(u'replaygain: analyzing {0} files'.format(len(items)))
        try:
            log.debug(u"replaygain: executing %s" % " ".join(cmd))
            output = call(cmd)
        except ReplayGainError as exc:
            log.warn(u'replaygain: analysis failed ({0})'.format(exc))
            return
        log.debug(u'replaygain: analysis finished')
        results = self.parse_tool_output(output, len(items) + (1 if is_album else 0))

        return results
    

    def parse_tool_output(self, text, num_lines):
        """Given the tab-delimited output from an invocation of mp3gain
        or aacgain, parse the text and return a list of dictionaries
        containing information about each analyzed file.
        """
        out = []
        for line in text.split('\n')[1:num_lines + 1]:
            parts = line.split('\t')
            d = {
                'file': parts[0],
                'mp3gain': int(parts[1]),
                'gain': float(parts[2]),
                'peak': float(parts[3]) / (1 << 15),
                'maxgain': int(parts[4]),
                'mingain': int(parts[5]),

            }
            out.append(Backend.Gain(d['gain'], d['peak']))
        return out


    @staticmethod
    def initialize_config(config):
        config.add({
            'command': u"",
            'noclip': True,
            'targetlevel': 89})

class GStreamerBackend(object):
    def __init__(self, config):
        self._src = Gst.ElementFactory.make("filesrc", "src")
        self._decbin = Gst.ElementFactory.make("decodebin", "decbin")
        self._conv = Gst.ElementFactory.make("audioconvert", "conv")
        self._res = Gst.ElementFactory.make("audioresample", "res")
        self._rg = Gst.ElementFactory.make("rganalysis", "rg")
        self._rg.set_property("forced", True)
        self._sink = Gst.ElementFactory.make("fakesink", "sink")
        
        self._pipe = Gst.Pipeline()
        self._pipe.add(self._src)
        self._pipe.add(self._decbin)
        self._pipe.add(self._conv)
        self._pipe.add(self._res)
        self._pipe.add(self._rg)
        self._pipe.add(self._sink)
        
        self._src.link(self._decbin)
        self._conv.link(self._res)
        self._res.link(self._rg)
        self._rg.link(self._sink)
        
        self._bus = self._pipe.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message::eos", self._on_eos)
        self._bus.connect("message::error", self._on_error)
        self._bus.connect("message::tag", self._on_tag)
        self._decbin.connect("pad-added", self._on_pad_added)
        self._decbin.connect("pad-removed", self._on_pad_removed)
        
        self._main_loop = GObject.MainLoop()

        self._files = []

    def compute(self, files, album):
        if len(self._files) != 0:
            raise Exception()


        self._files = list(files)
        
        if len(self._files) == 0:
            return

        self._file_tags = collections.defaultdict(dict)

        if album:
            self._rg.set_property("num-tracks", len(self._files))
        
        if self._set_first_file():
            self._main_loop.run()

    def compute_track_gain(self, items):
        self.compute(items, False)
        if len(self._file_tags) != len(items):
            raise Exception()

        ret = []
        for item in items:
            ret.append(Backend.Gain(self._file_tags[item]["TRACK_GAIN"], self._file_tags[item]["TRACK_PEAK"]))

        return ret

    def compute_album_gain(self, album):
        items = list(album.items())
        self.compute(items, True)
        if len(self._file_tags) != len(items):
            raise Exception()

        ret = []
        for item in items:
            ret.append(Backend.Gain(self._file_tags[item]["TRACK_GAIN"], self._file_tags[item]["TRACK_PEAK"]))

        last_tags = self._file_tags[items[-1]]
        return Backend.AlbumGain(Backend.Gain(last_tags["ALBUM_GAIN"], last_tags["ALBUM_PEAK"]), ret)

    def close(self):
        self._bus.remove_signal_watch()

    def _on_eos(self, bus, message):
        if not self._set_next_file():
            ret = self._pipe.set_state(Gst.State.NULL)
            self._main_loop.quit()
        

    def _on_error(self, bus, message):
        self._pipe.set_state(Gst.State.NULL)
        self._main_loop.quit()
        err, debug = message.parse_error()
        raise Exception("Error %s - %s on file %s" % (err, debug, self._src.get_property("location")))

    def _on_tag(self, bus, message):
        tags = message.parse_tag()

        def handle_tag(taglist, tag, userdata):
            if tag == Gst.TAG_TRACK_GAIN:
                self._file_tags[self._file]["TRACK_GAIN"] = taglist.get_double(tag)[1]
            elif tag == Gst.TAG_TRACK_PEAK:
                self._file_tags[self._file]["TRACK_PEAK"] = taglist.get_double(tag)[1]
            elif tag == Gst.TAG_ALBUM_GAIN:
                self._file_tags[self._file]["ALBUM_GAIN"] = taglist.get_double(tag)[1]
            elif tag == Gst.TAG_ALBUM_PEAK:
                self._file_tags[self._file]["ALBUM_PEAK"] = taglist.get_double(tag)[1]
            elif tag == Gst.TAG_REFERENCE_LEVEL:
                self._file_tags[self._file]["REFERENCE_LEVEL"] = taglist.get_double(tag)[1]
        
        tags.foreach(handle_tag, None)

    
    def _set_first_file(self):
        if len(self._files) == 0:
            return False
        
        self._file = self._files.pop(0)
        self._src.set_property("location", syspath(self._file.path))
        
        self._pipe.set_state(Gst.State.PLAYING)

        return True

    def _set_file(self):
        if len(self._files) == 0:
            return False
        
        self._file = self._files.pop(0)
        
        self._decbin.unlink(self._conv)
        self._decbin.set_state(Gst.State.READY)
        
        self._src.set_state(Gst.State.READY)
        self._src.set_property("location", syspath(self._file.path))
        
        self._src.sync_state_with_parent()
        self._src.get_state(Gst.CLOCK_TIME_NONE)
        self._decbin.sync_state_with_parent()
        self._decbin.get_state(Gst.CLOCK_TIME_NONE)
        
        return True
        
    
    def _set_next_file(self):
        self._pipe.set_state(Gst.State.PAUSED)
        self._pipe.get_state(Gst.CLOCK_TIME_NONE)
        
        ret = self._set_file()
        if ret:
            self._pipe.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
            self._pipe.set_state(Gst.State.PLAYING)

        return ret
        
        
    def _on_pad_added(self, decbin, pad):
        sink_pad = self._conv.get_compatible_pad(pad, None)
        if sink_pad is None:
            raise Exception()
        
        pad.link(sink_pad)

    
    def _on_pad_removed(self, decbin, pad):
        peer = pad.get_peer()
        if peer is not None:
            raise Exception()
    
    @staticmethod
    def initialize_config(config):
        global GObject, Gst

        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import GObject, Gst
        GObject.threads_init()
        Gst.init([sys.argv[0]])
        


class ReplayGainPlugin(BeetsPlugin):
    """Provides ReplayGain analysis.
    """

    BACKENDS = {
        "command"   : CommandBackend,
        "gstreamer" : GStreamerBackend
    }

    def __init__(self):
        super(ReplayGainPlugin, self).__init__()
        self.import_stages = [self.imported]

        self.config.add({
            'overwrite': False,
            'auto': True,
            'backend': u'',
        })

        self.overwrite = self.config['overwrite'].get(bool)
        self.automatic = self.config['auto'].get(bool)
        backend_name = self.config['backend'].get(unicode)
        if backend_name not in ReplayGainPlugin.BACKENDS:
            raise ui.UserError("Selected backend %s is not supported, please select one of: %s" % (backend_name, ReplayGainPlugin.BACKENDS.keys()))
        self.backend = ReplayGainPlugin.BACKENDS[backend_name].initialize_config(self.config)

        self.backend_instance = ReplayGainPlugin.BACKENDS[backend_name](self.config)

    
    def track_requires_gain(self, item):
        return self.overwrite or \
               (not item.rg_track_gain or not item.rg_track_peak)


    def album_requires_gain(self, album):
        # Skip calculating gain only when *all* files don't need
        # recalculation. This way, if any file among an album's tracks
        # needs recalculation, we still get an accurate album gain
        # value.
        return self.overwrite or \
               any([not item.rg_album_gain or not item.rg_album_peak for item in album.items()])


    def store_track_gain(self, item, track_gain):
        item.rg_track_gain = track_gain.gain
        item.rg_track_peak = track_gain.peak
        item.store()
            
        log.debug(u'replaygain: applied track gain {0}, peak {1}'.format(
            item.rg_track_gain,
            item.rg_track_peak
        ))

    def store_album_gain(self, album, album_gain):
        album.rg_album_gain = album_gain.gain
        album.rg_album_peak = album_gain.peak
        album.store()
        
        log.debug(u'replaygain: applied album gain {0}, peak {1}'.format(
            album.rg_album_gain,
            album.rg_album_peak
        ))


    def handle_album(self, album, write):
        if not self.album_requires_gain(album):
            log.info(u'Skipping album {0} - {1}'.format(album.albumartist,
                                                        album.album))
            return

        album_gain = self.backend_instance.compute_album_gain(album)
        if len(album_gain.track_gains) != len(album.items()):
            log.warn("ReplayGain backend failed for some tracks in album %s - %s" % (album.albumartist, album.album))
            return

        self.store_album_gain(album, album_gain.album_gain)
        for item, track_gain in itertools.izip(album.items(), album_gain.track_gains):
            self.store_track_gain(item, track_gain)
            if write:
                print "WRITING"
                item.write()
            
            

    def handle_track(self, item, write):
        if not self.track_requires_gain(item):
            log.info(u'Skipping track {0} - {1}'.format(item.artist,
                                                        item.title))
            return

        track_gains = self.backend_instance.compute_track_gain([item])
        if len(track_gains) != 1:
            log.warn("ReplayGain backend failed for track %s - %s" % (item.artist, item.title))
            return

        self.store_track_gain(item, track_gains[0])
        if write:
            item.write()
        

    def imported(self, session, task):
        """Our import stage function."""
        if not self.automatic:
            return

        if task.is_album:
            album = session.lib.get_album(task.album_id)
            self.handle_album(album, False)
        else:
            self.handle_track(task.item, False)


    def commands(self):
        """Provide a ReplayGain command."""
        def func(lib, opts, args):
            write = config['import']['write'].get(bool)

            if opts.album:
                for album in lib.albums(ui.decargs(args)):
                    # log.info(u'analyzing {0} - {1}'.format(album.albumartist,
                    #                                        album.album))
                    self.handle_album(album, write)

            else:
                # log.info(u'analyzing {0} - {1}'.format(item.artist,
                #                                        item.title))
                for item in lib.items(ui.decargs(args)):
                    self.handle_track(item, write)

        cmd = ui.Subcommand('replaygain', help='analyze for ReplayGain')
        cmd.parser.add_option('-a', '--album', action='store_true',
                              help='analyze albums instead of tracks')
        cmd.func = func
        return [cmd]



