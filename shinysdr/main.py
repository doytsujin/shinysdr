#!/usr/bin/env python

# Copyright 2013, 2014 Kevin Reid <kpreid@switchb.org>
#
# This file is part of ShinySDR.
# 
# ShinySDR is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# ShinySDR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with ShinySDR.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import, division

import argparse
import base64
import json
import logging
import os
import os.path
import shutil
import sys
import webbrowser
import __builtin__

from twisted.application.service import IService
from twisted.python import log
from twisted.internet import defer
from twisted.internet import reactor


class _Config(object):
	def __init__(self, reactor):
		self.reactor = reactor
		self._state_filename = None
		self.sources = _ConfigDict()
		self.databases = _ConfigDbs()
		self.accessories = _ConfigAccessories()
		self._make_service = None
	
	def _validate(self):
		if self._state_filename is None:
			raise Exception('Having no state file is not yet supported.')
		if self._make_service is None:
			raise Exception('Having no web service is not yet supported.')
	
	def persist_to_file(self, filename):
		self._state_filename = str(filename)

	def serve_web(self, http_endpoint, ws_endpoint, root_cap='%(root_cap)s'):
		# TODO: See if we're reinventing bits of Twisted service stuff here
		
		def make_service(top, noteDirty):
			import shinysdr.web
			return shinysdr.web.WebService({
					'databasesDir': self.databases._directory,
					'httpPort': http_endpoint,
					'wsPort': ws_endpoint,
					'rootCap': root_cap,
				}, top, noteDirty)
		
		self._make_service = make_service


class _ConfigDict(object):
	def __init__(self):
		self._values = {}

	def add(self, key, value):
		key = unicode(key)
		if key in self._values:
			raise KeyError('Key %r already present' % (key,))
		self._values[key] = value


class _ConfigAccessories(_ConfigDict):
	def add(self, key, value):
		import shinysdr.values
		
		if key in self._values:
			raise KeyError('Accessory key %r already present' % (key,))
		
		def f(r):
			self._values[key] = r
		
		self._values[key] = shinysdr.values.nullExportedState
		defer.maybeDeferred(lambda: value).addCallback(f)


class _ConfigDbs(object):
	def __init__(self):
		self._directory = None

	def add_directory(self, path):
		path = str(path)
		if self._directory is not None:
			raise Exception('Multiple database directories are not yet supported.')
		self._directory = path


def main(argv=None, _abort_for_test=False):
	if argv is None:
		argv = sys.argv
	
	# Configure logging. Some log messages would be discarded if we did not set up things early
	# TODO: Consult best practices for Python and Twisted logging.
	# TODO: Logs which are observably relevant should be sent to the client (e.g. the warning of refusing to have more receivers active)
	logging.basicConfig(level=logging.INFO)
	log.startLoggingWithObserver(log.PythonLoggingObserver(loggerName='shinysdr').emit, False)
	
	# Option parsing is done before importing the main modules so as to avoid the cost of initializing gnuradio if we are aborting early. TODO: Make that happen for createConfig too.
	argParser = argparse.ArgumentParser(prog=argv[0])
	argParser.add_argument('configFile', metavar='CONFIG',
		help='path of configuration file')
	argParser.add_argument('--create', dest='createConfig', action='store_true',
		help='write template configuration file to CONFIG and exit')
	argParser.add_argument('-g, --go', dest='openBrowser', action='store_true',
		help='open the UI in a web browser')
	argParser.add_argument('--force-run', dest='force_run', action='store_true',
		help='Run DSP even if no client is connected (for debugging).')
	args = argParser.parse_args(args=argv[1:])

	import shinysdr.top
	import shinysdr.source

	# Load config file
	if args.createConfig:
		with open(args.configFile, 'w') as f:
			f.write('''\
import shinysdr.plugins.osmosdr
import shinysdr.plugins.simulate

# OsmoSDR generic device source; handles USRP, RTL-SDR, FunCube
# Dongle, HackRF, etc.
# If desired, add sample_rate=<n> parameter.
# Use shinysdr.plugins.osmosdr.OsmoSDRProfile to set more parameters
# to make the best use of your specific hardware's capabilities.
config.sources.add(u'osmo', shinysdr.plugins.osmosdr.OsmoSDRSource(''))

# For hardware which uses a sound-card as its ADC or appears as an
# audio device.
config.sources.add(u'audio', shinysdr.source.AudioSource(''))

# Locally generated RF signals for test purposes.
config.sources.add(u'sim', shinysdr.plugins.simulate.SimulatedSource())

config.persist_to_file('state.json')

config.databases.add_directory('dbs/')

config.serve_web(
	# These are in Twisted endpoint description syntax:
	# <http://twistedmatrix.com/documents/current/api/twisted.internet.endpoints.html#serverFromString>
	# Note: ws_endpoint must currently be 1 greater than http_endpoint; if one
	# is SSL then both must be. These restrictions will be relaxed later.
	http_endpoint='tcp:8100',
	ws_endpoint='tcp:8101',

	# A secret placed in the URL as simple access control. Does not
	# provide any real security unless using HTTPS. The default value
	# in this file has been automatically generated from 128 random bits.
	# Set to None to not use any secret.
	root_cap='%(root_cap)s')
''' % {'root_cap': base64.urlsafe_b64encode(os.urandom(128 // 8)).replace('=', '')})
			sys.exit(0)
	else:
		configObj = _Config(reactor)
		
		# TODO: better ways to manage the namespaces?
		execfile(
			args.configFile,
			__builtin__.__dict__,
			{'shinysdr': shinysdr, 'config': configObj})
		configObj._validate()
		stateFile = configObj._state_filename
	
	def noteDirty():
		# just immediately write (revisit this when more performance is needed)
		with open(stateFile, 'w') as f:
			json.dump(top.state_to_json(), f)
	
	def restore(root, get_defaults):
		if os.path.isfile(stateFile):
			root.state_from_json(json.load(open(stateFile, 'r')))
			# make a backup in case this code version misreads the state and loses things on save (but only if the load succeeded, in case the file but not its backup is bad)
			shutil.copyfile(stateFile, stateFile + '~')
		else:
			root.state_from_json(get_defaults(root))
	
	log.msg('Constructing flow graph...')
	top = shinysdr.top.Top(
		sources=configObj.sources._values,
		accessories=configObj.accessories._values)
	
	log.msg('Restoring state...')
	restore(top, top_defaults)
	
	log.msg('Starting web server...')
	service = IService(configObj._make_service(top, noteDirty))
	service.startService()
	
	url = service.get_url()
	if args.openBrowser:
		log.msg('ShinySDR is ready. Opening ' + url)
		webbrowser.open(url, new=1, autoraise=True)
	else:
		log.msg('ShinySDR is ready. Visit ' + url)
	
	if args.force_run:
		log.msg('force_run')
		from gnuradio.gr import msg_queue
		top.add_audio_queue(msg_queue(limit=2), 44100)
		top.set_unpaused(True)
	
	if _abort_for_test:
		service.stopService()
		return top, noteDirty
	else:
		reactor.run()


def top_defaults(top):
	'''Return a friendly initial state for the top block using knowledge of the default config file.'''
	state = {}
	
	# TODO: fix fragility of assumptions
	sources = top.state()['source_name'].type().values()
	restricted = dict(sources)
	if 'audio' in restricted: del restricted['audio']  # typically not RF
	if 'sim' in restricted: del restricted['sim']  # would prefer the real thing
	if 'osmo' in restricted:
		state['source_name'] = 'osmo'
	elif len(restricted.keys()) > 0:
		state['source_name'] = restricted.keys()[0]
	# else out of ideas, let top block pick
	
	return state


if __name__ == '__main__':
	main()
