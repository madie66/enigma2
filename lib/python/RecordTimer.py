from __future__ import division
from __future__ import print_function
from boxbranding import getMachineBrand, getMachineName
import xml.etree.cElementTree
from bisect import insort
from datetime import datetime
from os import access, fsync, makedirs, remove, rename, statvfs, sys, W_OK
from os.path import exists, isdir, realpath, ismount
from sys import maxsize
import subprocess
import six
import threading
from time import ctime, localtime, strftime, time
from enigma import eEPGCache, getBestPlayableServiceReference, eServiceReferenceDVB, eStreamServer, eServiceReference, iRecordableService, quitMainloop, eActionMap, setPreferredTuner, eServiceCenter, pNavigation, eDVBFrontend

import NavigationInstance
import timer
from ServiceReference import ServiceReference
from Components.config import config
from Components.Harddisk import findMountPoint
from Components.UsageConfig import defaultMoviePath, calcFrontendPriorityIntval
from Components.TimerSanityCheck import TimerSanityCheck
import Components.RecordingConfig
Components.RecordingConfig.InitRecordingConfig()
from Screens.MessageBox import MessageBox
import Screens.Standby
from Tools.ServiceReference import service_types_tv_ref, service_types_radio_ref, serviceRefAppendPath
from Tools.Directories import fileExists, getRecordingFilename, resolveFilename, SCOPE_CONFIG
from Tools.ASCIItranslit import legacyEncode
from Tools import Trashcan
from Tools import Notifications
from Tools.XMLTools import stringToXML

# For descriptions etc we have:
# service reference  (to get the service name)
# name               (title)
# description        (description)
# event data         (ONLY for time adjustments etc.)

MODULE_NAME = __name__.split(".")[-1]

wasRecTimerWakeup = False
InfoBar = False

#//import later (no error message on system start)
#try:
#	from Screens.InfoBar import InfoBar
#except Exception, e:
#	print "[RecordTimer] import from 'Screens.InfoBar import InfoBar' failed:", e
#	InfoBar = False
#//

#+++
debug = False
#+++

#reset wakeup state after ending timer


def resetTimerWakeup():
	global wasRecTimerWakeup
	if exists("/tmp/was_rectimer_wakeup"):
		remove("/tmp/was_rectimer_wakeup")
		if debug:
			print("[RECORDTIMER] reset wakeup state")
	wasRecTimerWakeup = False

# Parses an event, and gives out a (begin, end, name, duration, eit)-tuple.
# The begin and end will be corrected to include margin padding.
#
def parseEvent(event, description=True):
	if description:
		name = event.getEventName()
		description = event.getShortDescription()
		if description == "":
			description = event.getExtendedDescription()
	else:
		name = ""
		description = ""

	# Replace linebreak's with spaces to avoid display issues in the text edit screens.
	# Enigma2 does not have a multiline InputBox or VirtualKeyBoard.
	description = description.replace("\n", " ")
	begin = event.getBeginTime()
	end = begin + event.getDuration()
	eit = event.getEventId()
	begin -= config.recording.margin_before.value * 60
	end += config.recording.margin_after.value * 60
	return (begin, end, name, description, eit)  # We should also report the margins!


class AFTEREVENT:
	def __init__(self):
		pass

	NONE = 0
	STANDBY = 1
	DEEPSTANDBY = 2
	AUTO = 3

	DEFAULT = int(config.recording.default_afterevent.value)


class TIMERTYPE:
	def __init__(self):
		pass

	JUSTPLAY = config.recording.default_timertype.value == "zap"
	ALWAYS_ZAP = config.recording.default_timertype.value == "zap+record"


def findSafeRecordPath(dirname):
	if not dirname:
		return None
	dirname = realpath(dirname)
	mountPoint = findMountPoint(dirname)
	if not ismount(mountPoint):
		print("[RecordTimer] Media is not mounted for '%s'." % dirname)
		return None
	if not isdir(dirname):
		try:
			makedirs(dirname)
		except OSError as err:
			print("[RecordTimer] Error %d: Failed to create dir '%s'!  (%s)" % (err.errno, dirname, err.strerror))
			return None
	return dirname


service_types_tv = service_types_tv_ref.toString()
service_types_radio = service_types_radio_ref.toString()


def getBqRootStr(ref):
	ref = ref.toString()
	if ref.startswith('1:0:2:'):           # we need that also?:----> or ref.startswith('1:0:10:'):
		service_types = service_types_radio
		if config.usage.multibouquet.value:
			bqrootstr = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "bouquets.radio" ORDER BY bouquet'
		else:
			bqrootstr = '%s FROM BOUQUET "userbouquet.favourites.radio" ORDER BY bouquet' % service_types
	else:
		service_types = service_types_tv
		if config.usage.multibouquet.value:
			bqrootstr = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "bouquets.tv" ORDER BY bouquet'
		else:
			bqrootstr = '%s FROM BOUQUET "userbouquet.favourites.tv" ORDER BY bouquet' % service_types

	return bqrootstr

# please do not translate log messages


def createRecordTimerEntry(timer):
	return RecordTimerEntry(timer.service_ref, timer.begin, timer.end, timer.name, timer.description,
		timer.eit, timer.disabled, timer.justplay, timer.afterEvent, dirname=timer.dirname,
		tags=timer.tags, descramble=timer.descramble, record_ecm=timer.record_ecm, always_zap=timer.always_zap,
		rename_repeat=timer.rename_repeat)


class RecordTimerEntry(timer.TimerEntry, object):
	def __init__(self, serviceref, begin, end, name, description, eit, disabled=False, justplay=TIMERTYPE.JUSTPLAY, afterEvent=AFTEREVENT.DEFAULT, checkOldTimers=False, dirname=None, tags=None, descramble='notset', record_ecm='notset', rename_repeat=True, isAutoTimer=False, ice_timer_id=None, always_zap=TIMERTYPE.ALWAYS_ZAP, MountPath=None, fixDescription=False):
		timer.TimerEntry.__init__(self, int(begin), int(end))
		if checkOldTimers:
			if self.begin < time() - 1209600:
				self.begin = int(time())
		if self.end < self.begin:
			self.end = self.begin
		if not isinstance(serviceref, ServiceReference):
			raise AssertionError("invalid serviceref")
		if serviceref and serviceref.isRecordable():
			self.service_ref = serviceref
		else:
			self.service_ref = ServiceReference(None)
		self.dontSave = False
		self.eit = None
		if not description or not name or not eit:
			evt = self.getEventFromEPGId(eit) or self.getEventFromEPG()
			if evt:
				if not description:
					description = evt.getShortDescription()
				if not description:
					description = evt.getExtendedDescription()
				if description and fixDescription:
					# Replace linebreak's with spaces to avoid display issues in the text edit screens.
					# Enigma2 does not have a multiline InputBox or VirtualKeyBoard.
					description = description.replace("\n", " ")
				if not name:
					name = evt.getEventName()
				if not eit:
					eit = evt.getEventId()
		self.eit = eit
		self.name = name
		self.description = description
		self.disabled = disabled
		self.timer = None
		self.__record_service = None
		self.start_prepare = 0
		self.justplay = justplay
		self.always_zap = always_zap
		self.afterEvent = afterEvent
		self.dirname = dirname
		self.dirnameHadToFallback = False
		self.autoincrease = False
		self.autoincreasetime = 3600 * 24  # 1 day
		self.tags = tags or []
		self.MountPath = None
		self.messageString = ""
		self.messageStringShow = False
		self.messageBoxAnswerPending = False
		self.justTriedFreeingTuner = False
		self.MountPathRetryCounter = 0
		self.MountPathErrorNumber = 0
		self.lastend = 0

		if descramble == 'notset' and record_ecm == 'notset':
			if config.recording.ecm_data.value == 'descrambled+ecm':
				self.descramble = True
				self.record_ecm = True
			elif config.recording.ecm_data.value == 'scrambled+ecm':
				self.descramble = False
				self.record_ecm = True
			elif config.recording.ecm_data.value == 'normal':
				self.descramble = True
				self.record_ecm = False
		else:
			self.descramble = descramble
			self.record_ecm = record_ecm

		config.usage.frontend_priority_intval.setValue(calcFrontendPriorityIntval(config.usage.frontend_priority, config.usage.frontend_priority_multiselect, config.usage.frontend_priority_strictly))
		config.usage.recording_frontend_priority_intval.setValue(calcFrontendPriorityIntval(config.usage.recording_frontend_priority, config.usage.recording_frontend_priority_multiselect, config.usage.recording_frontend_priority_strictly))
		self.needChangePriorityFrontend = config.usage.recording_frontend_priority_intval.value != "-2" and config.usage.recording_frontend_priority_intval.value != config.usage.frontend_priority_intval.value
		self.change_frontend = False
		self.rename_repeat = rename_repeat
		self.isAutoTimer = isAutoTimer
		self.ice_timer_id = ice_timer_id
		self.wasInStandby = False

		#workaround for vmc crash - only a dummy entry!!!
		self.justremind = False
		'''
		File "/usr/lib/enigma2/python/Plugins/Extensions/VMC/VMC_Classes.py", line 3704, in TimerChange
		"Filename") and not timer.justplay and not timer.justremind and timer.state == TimerEntry.StateEnded:
		AttributeError: 'RecordTimerEntry' object has no attribute 'justremind'
		'''
		###

		self.log_entries = []
		self.check_justplay()
		self.resetState()

	def __repr__(self):
		ice = ""
		if self.ice_timer_id:
			ice = ", ice_timer_id=%s" % self.ice_timer_id
		disabled = ""
		if self.disabled:
			disabled = ", Disabled"
		return "RecordTimerEntry(name=%s, begin=%s, end=%s, serviceref=%s, justplay=%s, isAutoTimer=%s%s%s)" % (self.name, ctime(self.begin), ctime(self.end), self.service_ref, self.justplay, self.isAutoTimer, ice, disabled)

	def log(self, code, msg):
		self.log_entries.append((int(time()), code, msg))
		print("[TIMER]", msg)

	def MountTest(self, dirname, cmd):
		if cmd == 'writeable':
			if not access(dirname, W_OK):
				self.stop_MountTest(None, cmd)
		elif cmd == 'freespace':
			s = statvfs(dirname)
			if (s.f_bavail * s.f_bsize) // 1000000 < 1024:
				self.stop_MountTest(None, cmd)

	def stop_MountTest(self, thread, cmd):
		if thread and thread.isAlive():
			print('timeout thread : %s' % cmd)
			thread._Thread__stop()

		if cmd == 'writeable':
			self.MountPathErrorNumber = 2
		elif cmd == 'freespace':
			self.MountPathErrorNumber = 3

	def freespace(self, WRITEERROR=False):
		if WRITEERROR:
			dirname = self.MountPath
			if findSafeRecordPath(dirname) is None:
				return ("mount '%s' is not available." % dirname, 1)
		else:
			self.MountPath = None
			if not self.dirname:
				dirname = findSafeRecordPath(defaultMoviePath())
			else:
				dirname = findSafeRecordPath(self.dirname)
				if dirname is None:
					dirname = findSafeRecordPath(defaultMoviePath())
					self.dirnameHadToFallback = True
			if not dirname:
				dirname = self.dirname
				if not dirname:
					dirname = defaultMoviePath() or '-'
				self.log(0, "Mount '%s' is not available." % dirname)
				self.MountPathErrorNumber = 1
				return False

		self.MountPathErrorNumber = 0
		for cmd in ('writeable', 'freespace'):
			print('starting thread :%s' % cmd)
			p = threading.Thread(target=self.MountTest, args=(dirname, cmd))
			t = threading.Timer(3, self.stop_MountTest, args=(p, cmd))
			t.start()
			p.start()
			p.join()
			t.cancel()
			if self.MountPathErrorNumber:
				print('break - error number: %d' % self.MountPathErrorNumber)
				break
			print('finished thread :%s' % cmd)

		if WRITEERROR:
			if self.MountPathErrorNumber == 2:
				return ("mount '%s' is not writeable." % dirname, 2)
			elif self.MountPathErrorNumber == 3:
				return ("mount '%s' has not enough free space to record." % dirname, 3)
			else:
				return ("unknown error.", 0)

		if self.MountPathErrorNumber == 2:
			self.log(0, "Mount '%s' is not writeable." % dirname)
			return False
		elif self.MountPathErrorNumber == 3:
			self.log(0, _("Mount '%s' has not enough free space to record.") % dirname)
			return False
		else:
			if debug:
				self.log(0, "Found enough free space to record")
			self.MountPathRetryCounter = 0
			self.MountPathErrorNumber = 0
			self.MountPath = dirname
			return True

	def calculateFilename(self, name=None):
		service_name = self.service_ref.getServiceName()
		begin_date = strftime("%Y%m%d %H%M", localtime(self.begin))
		name = name or self.name
		filename = begin_date + " - " + service_name
		if name:
			if config.recording.filename_composition.value == "veryveryshort":
				filename = name
			elif config.recording.filename_composition.value == "veryshort":
				filename = name + " - " + begin_date
			elif config.recording.filename_composition.value == "short":
				filename = strftime("%Y%m%d", localtime(self.begin)) + " - " + name
			elif config.recording.filename_composition.value == "shortwithtime":
				filename = strftime("%Y%m%d %H%M", localtime(self.begin)) + " - " + name
			elif config.recording.filename_composition.value == "long":
				filename += " - " + name + " - " + self.description
			else:
				filename += " - " + name # standard

		if config.recording.ascii_filenames.value:
			filename = legacyEncode(filename)

		self.Filename = getRecordingFilename(filename, self.MountPath)

		if debug:
			self.log(0, "Filename calculated as: '%s'" % self.Filename)
		return self.Filename

	def getEventFromEPGId(self, id=None):
		id = id or self.eit
		epgcache = eEPGCache.getInstance()
		ref = self.service_ref and self.service_ref.ref
		return id and epgcache.lookupEventId(ref, id) or None

	def getEventFromEPG(self):
		epgcache = eEPGCache.getInstance()
		queryTime = self.begin + (self.end - self.begin) // 2
		ref = self.service_ref and self.service_ref.ref
		return epgcache.lookupEventTime(ref, queryTime)

	def tryPrepare(self):
		if self.justplay:
			return True
		else:
			if not self.calculateFilename():
				self.do_backoff()
				self.start_prepare = int(time() + self.backoff)
				return False
			rec_ref = self.service_ref and self.service_ref.ref
			if rec_ref and rec_ref.flags & eServiceReference.isGroup:
				rec_ref = getBestPlayableServiceReference(rec_ref, eServiceReference())
				if not rec_ref:
					self.log(1, "The 'get best playable service for group... record' call failed!")
					return False

			self.setRecordingPreferredTuner()
			self.record_service = rec_ref and NavigationInstance.instance.recordService(rec_ref, False, pNavigation.isRealRecording)

			if not self.record_service:
				self.log(1, "The 'record service' call failed!")
				self.setRecordingPreferredTuner(setdefault=True)
				return False

			name = self.name
			description = self.description
			if self.repeated:
				epgcache = eEPGCache.getInstance()
				queryTime = self.begin + (self.end - self.begin) // 2
				evt = epgcache.lookupEventTime(rec_ref, queryTime)
				if evt:
					if self.rename_repeat:
						event_description = evt.getShortDescription()
						if not event_description:
							event_description = evt.getExtendedDescription()
						if event_description and event_description != description:
							description = event_description
						event_name = evt.getEventName()
						if event_name and event_name != name:
							name = event_name
							if not self.calculateFilename(event_name):
								self.do_backoff()
								self.start_prepare = int(time() + self.backoff)
								return False
					event_id = evt.getEventId()
				else:
					event_id = -1
			else:
				event_id = self.eit
				if event_id is None:
					event_id = -1

			prep_res = self.record_service.prepare(self.Filename + self.record_service.getFilenameExtension(), self.begin, self.end, event_id, name.replace("\n", " "), description.replace("\n", " "), " ".join(self.tags), bool(self.descramble), bool(self.record_ecm))
			if prep_res:
				if prep_res == -255:
					self.log(4, "Failed to write meta information!")
				else:
					self.log(2, "The 'prepare' call failed with error %d!" % prep_res)

				# we must calc nur start time before stopRecordService call because in Screens/Standby.py TryQuitMainloop tries to get
				# the next start time in evEnd event handler...
				self.do_backoff()
				self.start_prepare = int(time() + self.backoff)

				NavigationInstance.instance.stopRecordService(self.record_service)
				self.record_service = None
				self.setRecordingPreferredTuner(setdefault=True)
				return False
			return True

	def do_backoff(self):
		if self.backoff == 0:
			self.backoff = 5
		else:
			self.backoff *= 2
			if self.backoff > 100:
				self.backoff = 100
		self.log(10, "Backoff, retry in %d seconds." % self.backoff)

	def activate(self):
		global wasRecTimerWakeup, InfoBar

		if not InfoBar:
			try:
				from Screens.InfoBar import InfoBar
			except Exception as e:
				print("[RecordTimer] import from 'Screens.InfoBar import InfoBar' failed:", e)

		if exists("/tmp/was_rectimer_wakeup") and not wasRecTimerWakeup:
			wasRecTimerWakeup = int(open("/tmp/was_rectimer_wakeup", "r").read()) and True or False

		next_state = self.state + 1
		if debug:
			self.log(5, "Activating state %d." % next_state)

		# print "[TIMER] activate called",time(),next_state,self.first_try_prepare,' pending ',self.messageBoxAnswerPending,' justTried ',self.justTriedFreeingTuner,' show ',self.messageStringShow,self.messageString #TODO remove

		if next_state == self.StatePrepared:
			if self.messageBoxAnswerPending:
				self.start_prepare = int(time()) + 1 # call again in 1 second
				return False

			if self.justTriedFreeingTuner:
				self.start_prepare = int(time()) + 5 # tryPrepare in 5 seconds
				self.justTriedFreeingTuner = False
				return False

			if not self.justplay and not self.freespace():
				if self.MountPathErrorNumber < 3 and self.MountPathRetryCounter < 3:
					self.MountPathRetryCounter += 1
					self.start_prepare = int(time()) + 5 # tryPrepare in 5 seconds
					self.log(0, "next try in 5 seconds ...(%d/3)" % self.MountPathRetryCounter)
					return False
				message = _("Write error at start of recording. %s\n%s") % ((_("Disk was not found!"), _("Disk is not writable!"), _("Disk full?"))[self.MountPathErrorNumber - 1], self.name)
				messageboxtyp = MessageBox.TYPE_ERROR
				timeout = 20
				id = "DiskFullMessage"
				if InfoBar and InfoBar.instance:
					InfoBar.instance.openInfoBarMessage(message, messageboxtyp, timeout)
				else:
					Notifications.AddPopup(message, messageboxtyp, timeout=timeout, id=id)
				self.failed = True
				self.next_activation = time()
				self.lastend = self.end
				self.end = time() + 5
				self.backoff = 0
				return True

			if self.always_zap:
				Screens.Standby.TVinStandby.skipHdmiCecNow('zapandrecordtimer')
				if Screens.Standby.inStandby:
					self.wasInStandby = True
					#eActionMap.getInstance().bindAction('', -sys.maxsize - 1, self.keypress)
					#set service to zap after standby
					Screens.Standby.inStandby.prev_running_service = self.service_ref.ref
					Screens.Standby.inStandby.paused_service = None
					#wakeup standby
					Screens.Standby.inStandby.Power()
					self.log(5, "Wakeup and zap to recording service")
				else:
					cur_zap_ref = NavigationInstance.instance.getCurrentlyPlayingServiceReference()
					if cur_zap_ref and not cur_zap_ref.getPath():# we do not zap away if it is no live service
						self.setRecordingPreferredTuner()
						self.failureCB(True)
						self.log(5, "zap to recording service")

			if self.tryPrepare():
				if debug:
					self.log(6, "Prepare okay, waiting for begin")
				if self.messageStringShow:
					message = _("In order to record a timer, a tuner was freed successfully:\n\n") + self.messageString
					messageboxtyp = MessageBox.TYPE_INFO
					timeout = 20
					if InfoBar and InfoBar.instance:
						InfoBar.instance.openInfoBarMessage(message, messageboxtyp, timeout)
					else:
						Notifications.AddNotification(MessageBox, message, messageboxtyp, timeout=timeout)
				# create file to "reserve" the filename
				# because another recording at the same time on another service can try to record the same event
				# i.e. cable / sat.. then the second recording needs an own extension... when we create the file
				# here than calculateFilename is happy
				if not self.justplay:
					open(self.Filename + self.record_service.getFilenameExtension(), "w").close()
					# Give the Trashcan a chance to clean up
					# Need try/except as Trashcan.instance may not exist
					# for a missed recording started at boot-time.
					try:
						Trashcan.instance.cleanIfIdle()
					except Exception as e:
						print("[RecordTimer] Failed to call Trashcan.instance.cleanIfIdle()!")
						print("[RecordTimer] Error: %s" % str(e))
				# fine. it worked, resources are allocated.
				self.next_activation = self.begin
				self.backoff = 0
				return True
			self.log(7, "Prepare failed!")
			if eStreamServer.getInstance().getConnectedClients():
				eStreamServer.getInstance().stopStream()
				return False

			if self.first_try_prepare == 0:
				# (0) try to make a tuner available by disabling PiP
				self.first_try_prepare += 1
				if not InfoBar:
					from Screens.InfoBar import InfoBar
				from Screens.InfoBarGenerics import InfoBarPiP
				from Components.ServiceEventTracker import InfoBarCount
				InfoBarInstance = InfoBarCount == 1 and InfoBar.instance
				if InfoBarInstance and InfoBarPiP.pipShown(InfoBarInstance) == True:
					if config.recording.ask_to_abort_pip.value == "ask":
						self.log(8, "asking user to disable PiP")
						self.messageBoxAnswerPending = True
						callback = self.failureCB_pip
						message = _("A timer failed to record!\nDisable PiP and try again?\n")
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = 20
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
					elif config.recording.ask_to_abort_pip.value in ("abort_no_msg", "abort_msg"):
						self.log(8, "disable PiP without asking")
						self.setRecordingPreferredTuner()
						self.failureCB_pip(True)
					return False
				else:
					self.log(8, "currently no PiP active... so we dont need to stop it")

			if self.first_try_prepare == 1:
				# (1) try to make a tuner available by aborting pseudo recordings
				self.first_try_prepare += 1
				self.backoff = 0
				if len(NavigationInstance.instance.getRecordings(False, pNavigation.isPseudoRecording)) > 0:
					if config.recording.ask_to_abort_pseudo_rec.value == "ask":
						self.log(8, "asking user to abort pseudo recordings")
						self.messageBoxAnswerPending = True
						callback = self.failureCB_pseudo_rec
						message = _("A timer failed to record!\nAbort pseudo recordings (e.g. EPG refresh) and try again?\n")
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = 20
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
					elif config.recording.ask_to_abort_pseudo_rec.value in ("abort_no_msg", "abort_msg"):
						self.log(8, "abort pseudo recordings without asking")
						self.setRecordingPreferredTuner()
						self.failureCB_pseudo_rec(True)
					return False
				else:
					self.log(8, "currently no pseudo recordings active... so we dont need to stop it")

			if self.first_try_prepare == 2:
				# (2) try to make a tuner available by aborting streaming
				self.first_try_prepare += 1
				self.backoff = 0
				if len(NavigationInstance.instance.getRecordings(False, pNavigation.isStreaming)) > 0:
					if config.recording.ask_to_abort_streaming.value == "ask":
						self.log(8, "asking user to abort streaming")
						self.messageBoxAnswerPending = True
						callback = self.failureCB_streaming
						message = _("A timer failed to record!\nAbort streaming and try again?\n")
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = 20
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
					elif config.recording.ask_to_abort_streaming.value in ("abort_no_msg", "abort_msg"):
						self.log(8, "abort streaming without asking")
						self.setRecordingPreferredTuner()
						self.failureCB_streaming(True)
					return False
				else:
					self.log(8, "currently no streaming active... so we dont need to stop it")

			if self.first_try_prepare == 3:
				# (3) try to make a tuner available by switching live TV to the recording service
				self.first_try_prepare += 1
				self.backoff = 0
				cur_ref = NavigationInstance.instance.getCurrentlyPlayingServiceReference()
				if cur_ref and not cur_ref.getPath():
					if Screens.Standby.inStandby:
						self.setRecordingPreferredTuner()
						self.failureCB(True)
					elif not config.recording.asktozap.value:
						self.log(8, "Asking user to zap away")
						self.messageBoxAnswerPending = True
						callback = self.failureCB
						message = _("A timer failed to record!\nDisable TV and try again?\n")
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = 20
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
					else: # zap without asking
						self.log(9, "Zap without asking")
						self.setRecordingPreferredTuner()
						self.failureCB(True)
					return False
				elif cur_ref:
					self.log(8, "Currently running service is not a live service.. so stopping it makes no sense")
				else:
					self.log(8, "Currently no service running... so we dont need to stop it")

			if self.first_try_prepare == 4:
				# (4) freeing a tuner failed
				self.first_try_prepare += 1
				self.log(8, "freeing a tuner failed")
				if self.messageString:
					message = _("No tuner is available for recording a timer!\n\nThe following methods of freeing a tuner were tried without success:\n\n") + self.messageString
				else:
					message = _("No tuner is available for recording a timer!\n")
				messageboxtyp = MessageBox.TYPE_INFO
				timeout = 20
				if InfoBar and InfoBar.instance:
					InfoBar.instance.openInfoBarMessage(message, messageboxtyp, timeout)
				else:
					Notifications.AddNotification(MessageBox, message, messageboxtyp, timeout=timeout)

			return False

		elif next_state == self.StateRunning:
			# if this timer has been cancelled, just go to "end" state.
			if self.cancelled:
				return True

			if self.failed:
				return True

			if self.justplay:
				Screens.Standby.TVinStandby.skipHdmiCecNow('zaptimer')
				if Screens.Standby.inStandby:
					self.wasInStandby = True
					#eActionMap.getInstance().bindAction('', -maxsize - 1, self.keypress)
					self.log(11, "Wake up and zap")
					#set service to zap after standby
					Screens.Standby.inStandby.prev_running_service = self.service_ref.ref
					Screens.Standby.inStandby.paused_service = None
					#wakeup standby
					Screens.Standby.inStandby.Power()
				else:
					self.log(11, _("zapping"))
					found = False
					notFound = False
					NavigationInstance.instance.isMovieplayerActive()
					from Screens.ChannelSelection import ChannelSelection
					ChannelSelectionInstance = ChannelSelection.instance
					if ChannelSelectionInstance:
						bqrootstr = getBqRootStr(self.service_ref.ref)
						rootstr = ''
						serviceHandler = eServiceCenter.getInstance()
						rootbouquet = eServiceReference(bqrootstr)
						bouquet = eServiceReference(bqrootstr)
						bouquetlist = serviceHandler.list(bouquet)
						# we need a way out of the loop,
						# if channel is not in bouquets
						bouquetcount = 0
						bouquets = []
						if not bouquetlist is None:
							while True:
								bouquet = bouquetlist.getNext()
								# can we make it easier?
								# or found a way to make another way for that
								if bouquets == []:
									bouquets.append(bouquet)
								else:
									for x in bouquets:
										if x != bouquet:
											bouquets.append(bouquet)
										else:
											bouquetcount += 1
								if bouquetcount >= 5:
									notFound = True
									break

								if bouquet.flags & eServiceReference.isDirectory:
									ChannelSelectionInstance.clearPath()
									ChannelSelectionInstance.setRoot(bouquet)
									servicelist = serviceHandler.list(bouquet)
									if not servicelist is None:
										serviceIterator = servicelist.getNext()
										while serviceIterator.valid():
											if self.service_ref.ref == serviceIterator:
												break
											serviceIterator = servicelist.getNext()
										if self.service_ref.ref == serviceIterator:
											break
							if found:
								ChannelSelectionInstance.enterPath(rootbouquet)
								ChannelSelectionInstance.enterPath(bouquet)
								ChannelSelectionInstance.saveRoot()
								ChannelSelectionInstance.saveChannel(self.service_ref.ref)
						if found:
							ChannelSelectionInstance.addToHistory(self.service_ref.ref)
					if notFound:
						# Can we get a result for that ?
						# see if you want to delete the running Timer
						self.switchToAll()
					else:
						NavigationInstance.instance.playService(self.service_ref.ref)
				return True
			else:
				self.log(11, _("start recording"))
				record_res = self.record_service.start()
				self.setRecordingPreferredTuner(setdefault=True)
				if record_res:
					self.log(13, "Start recording error %d!" % record_res)
					self.do_backoff()
					# retry
					self.begin = time() + self.backoff
					return False
				return True

		elif next_state == self.StateEnded or next_state == self.StateFailed:
			old_end = self.end
			if self.setAutoincreaseEnd():
				self.log(12, "Auto increase recording length %d minute(s)." % int((self.end - old_end) / 60))
				self.state -= 1
				return True
			if self.justplay:
				self.log(12, _("end zapping"))
			else:
				self.log(12, _("stop recording"))
			if not self.justplay:
				if self.record_service:
					NavigationInstance.instance.stopRecordService(self.record_service)
					self.record_service = None
			if self.lastend and self.failed:
				self.end = self.lastend

			NavigationInstance.instance.RecordTimer.saveTimer()

			box_instandby = Screens.Standby.inStandby
			tv_notactive = Screens.Standby.TVinStandby.getTVstate('notactive')
			isRecordTime = abs(NavigationInstance.instance.RecordTimer.getNextRecordingTime() - time()) <= 900 or NavigationInstance.instance.RecordTimer.getStillRecording()

			if debug:
				print("[RECORDTIMER] box_instandby=%s" % box_instandby, "tv_notactive=%s" % tv_notactive, "wasRecTimerWakeup=%s" % wasRecTimerWakeup, "self.wasInStandby=%s" % self.wasInStandby, "self.afterEvent=%s" % self.afterEvent, "isRecordTime=%s" % isRecordTime)

			if self.afterEvent == AFTEREVENT.STANDBY or (self.afterEvent == AFTEREVENT.AUTO and self.wasInStandby and (not wasRecTimerWakeup or (wasRecTimerWakeup and isRecordTime))):
				if not box_instandby and not tv_notactive:# not already in standby
					callback = self.sendStandbyNotification
					message = _("A finished record timer wants to set your\n%s %s to standby. Do that now?") % (getMachineBrand(), getMachineName())
					messageboxtyp = MessageBox.TYPE_YESNO
					timeout = int(config.usage.shutdown_msgbox_timeout.value)
					default = True
					if InfoBar and InfoBar.instance:
						InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
					else:
						Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
				elif not box_instandby:
					self.sendStandbyNotification(True)

			if isRecordTime or abs(NavigationInstance.instance.RecordTimer.getNextZapTime() - time()) <= 900:
				if self.afterEvent == AFTEREVENT.DEEPSTANDBY or (wasRecTimerWakeup and self.afterEvent == AFTEREVENT.AUTO and self.wasInStandby) or (self.afterEvent == AFTEREVENT.AUTO and wasRecTimerWakeup):
					print('[Timer] Recording or Recording due is next 15 mins, not return to deepstandby')
				self.wasInStandby = False
				return True
			elif abs(NavigationInstance.instance.PowerTimer.getNextPowerManagerTime() - time()) <= 900 or NavigationInstance.instance.PowerTimer.isProcessing(exceptTimer=0) or not NavigationInstance.instance.PowerTimer.isAutoDeepstandbyEnabled():
				if self.afterEvent == AFTEREVENT.DEEPSTANDBY or (wasRecTimerWakeup and self.afterEvent == AFTEREVENT.AUTO and self.wasInStandby) or (self.afterEvent == AFTEREVENT.AUTO and wasRecTimerWakeup):
					print('[Timer] PowerTimer due is next 15 mins or is actual currently active, not return to deepstandby')
				self.wasInStandby = False
				resetTimerWakeup()
				return True

			if self.afterEvent == AFTEREVENT.DEEPSTANDBY or (wasRecTimerWakeup and self.afterEvent == AFTEREVENT.AUTO and self.wasInStandby):
				if not Screens.Standby.inTryQuitMainloop: # no shutdown messagebox is open
					if not box_instandby and not tv_notactive: # not already in standby
						callback = self.sendTryQuitMainloopNotification
						message = _("A finished record timer wants to shut down\nyour %s %s. Shutdown now?") % (getMachineBrand(), getMachineName())
						messageboxtyp = MessageBox.TYPE_YESNO
						timeout = int(config.usage.shutdown_msgbox_timeout.value)
						default = True
						if InfoBar and InfoBar.instance:
							InfoBar.instance.openInfoBarMessageWithCallback(callback, message, messageboxtyp, timeout, default)
						else:
							Notifications.AddNotificationWithCallback(callback, MessageBox, message, messageboxtyp, timeout=timeout, default=default)
					else:
						print("[RecordTimer] quitMainloop #1")
						quitMainloop(1)
			elif self.afterEvent == AFTEREVENT.AUTO and wasRecTimerWakeup:
				if not Screens.Standby.inTryQuitMainloop: # no shutdown messagebox is open
					if Screens.Standby.inStandby: # in standby
						print("[RecordTimer] quitMainloop #2")
						quitMainloop(1)
			self.wasInStandby = False
			resetTimerWakeup()
			return True

	def keypress(self, key=None, flag=1):
		if flag and self.wasInStandby:
			self.wasInStandby = False
			eActionMap.getInstance().unbindAction('', self.keypress)

	def setAutoincreaseEnd(self, entry=None):
		if not self.autoincrease:
			return False
		if entry is None:
			new_end = int(time()) + self.autoincreasetime
		else:
			new_end = entry.begin - 30

		dummyentry = RecordTimerEntry(self.service_ref, self.begin, new_end, self.name, self.description, self.eit,
		disabled=True, justplay=self.justplay, afterEvent=self.afterEvent,
		dirname=self.dirname, tags=self.tags)
		dummyentry.disabled = self.disabled
		timersanitycheck = TimerSanityCheck(NavigationInstance.instance.RecordTimer.timer_list, dummyentry)
		if not timersanitycheck.check():
			simulTimerList = timersanitycheck.getSimulTimerList()
			if simulTimerList is not None and len(simulTimerList) > 1:
				new_end = simulTimerList[1].begin
				new_end -= 30				# allow 30 seconds for prepare
		if new_end <= time():
			return False
		self.end = new_end
		return True

	def setRecordingPreferredTuner(self, setdefault=False):
		if self.needChangePriorityFrontend:
			elem = None
			if not self.change_frontend and not setdefault:
				elem = config.usage.recording_frontend_priority_intval.value
				self.change_frontend = True
			elif self.change_frontend and setdefault:
				elem = config.usage.frontend_priority_intval.value
				self.change_frontend = False
			if elem is not None:
				setPreferredTuner(int(elem))

	def sendStandbyNotification(self, answer):
		if answer:
			session = Screens.Standby.Standby
			option = None
			if InfoBar and InfoBar.instance:
				InfoBar.instance.openInfoBarSession(session, option)
			else:
				Notifications.AddNotification(session)

	def sendTryQuitMainloopNotification(self, answer):
		if answer:
			session = Screens.Standby.TryQuitMainloop
			option = 1
			if InfoBar and InfoBar.instance:
				InfoBar.instance.openInfoBarSession(session, option)
			else:
				Notifications.AddNotification(session, option)

	def getNextActivation(self, getNextStbPowerOn=False):
		self.isStillRecording = False
		next_state = self.state + 1
		if getNextStbPowerOn:
			if next_state == 3:
				self.isStillRecording = True
				next_day = 0
				count_day = 0
				wd_timer = datetime.fromtimestamp(self.begin).isoweekday() * -1
				wd_repeated = bin(128 + int(self.repeated))

				for s in list(range(wd_timer - 1, -8, -1)):
					count_day += 1
					if int(wd_repeated[s]):
						next_day = s
						break
				if next_day == 0:
					for s in list(range(-1, wd_timer - 1, -1)):
						count_day += 1
						if int(wd_repeated[s]):
							next_day = s
							break
				#return self.begin + 86400 * count_day
				return self.start_prepare + 86400 * count_day
			elif next_state == 2:
				return self.begin
			elif next_state == 1:
				return self.start_prepare
			else:
				return -1

		if self.state == self.StateEnded or self.state == self.StateFailed:
			if self.end > time():
				self.isStillRecording = True
			return self.end
		if next_state == self.StateEnded or next_state == self.StateFailed:
			if self.end > time():
				self.isStillRecording = True
		return {self.StatePrepared: self.start_prepare,
				self.StateRunning: self.begin,
				self.StateEnded: self.end}[next_state]

	def failureCB_pip(self, answer):
		if answer:
			self.log(13, "ok, disable PiP")
			global InfoBar
			if not InfoBar:
				from Screens.InfoBar import InfoBar
			from Screens.InfoBarGenerics import InfoBarPiP
			from Components.ServiceEventTracker import InfoBarCount
			InfoBarInstance = InfoBarCount == 1 and InfoBar.instance
			if InfoBarInstance:
				InfoBarPiP.showPiP(InfoBarInstance)
				self.messageString += _("Disabled PiP.\n")
			else:
				self.log(14, "tried to disable PiP, suddenly found no InfoBar.instance")
				self.messageString += _("Tried to disable PiP, suddenly found no InfoBar.instance.\n")
			if config.recording.ask_to_abort_pip.value in ("ask", "abort_msg"):
				self.messageStringShow = True
			self.justTriedFreeingTuner = True
		else:
			self.log(14, "user didn't want to disable PiP, try other methods of freeing a tuner")
		self.messageBoxAnswerPending = False

	def failureCB_pseudo_rec(self, answer):
		if answer:
			self.log(13, "ok, abort pseudo recordings")
			for rec in NavigationInstance.instance.getRecordings(False, pNavigation.isPseudoRecording):
				NavigationInstance.instance.stopRecordService(rec)
				self.messageString += _("Aborted a pseudo recording.\n")
			if config.recording.ask_to_abort_pseudo_rec.value in ("ask", "abort_msg"):
				self.messageStringShow = True
			self.justTriedFreeingTuner = True
		else:
			self.log(14, "user didn't want to abort pseudo recordings, try other methods of freeing a tuner")
		self.messageBoxAnswerPending = False

	def failureCB_streaming(self, answer):
		if answer:
			self.log(13, "ok, abort streaming")
			for rec in NavigationInstance.instance.getRecordings(False, pNavigation.isStreaming):
				NavigationInstance.instance.stopRecordService(rec)
				self.messageString += _("Aborted a streaming service.\n")
			if config.recording.ask_to_abort_streaming.value in ("ask", "abort_msg"):
				self.messageStringShow = True
			self.justTriedFreeingTuner = True
		else:
			self.log(14, "user didn't want to abort streaming, try other methods of freeing a tuner")
		self.messageBoxAnswerPending = False

	def failureCB(self, answer):
		if answer:
			self.log(13, "ok, zapped away")
			self.messageString += _("The TV was switched to the recording service!\n")
			self.messageStringShow = True
			found = False
			notFound = False
			#NavigationInstance.instance.stopUserServices()
			from Screens.ChannelSelection import ChannelSelection
			ChannelSelectionInstance = ChannelSelection.instance
			if ChannelSelectionInstance:
				bqrootstr = getBqRootStr(self.service_ref.ref)
				rootstr = ''
				serviceHandler = eServiceCenter.getInstance()
				rootbouquet = eServiceReference(bqrootstr)
				bouquet = eServiceReference(bqrootstr)
				bouquetlist = serviceHandler.list(bouquet)
				# we need a way out of the loop,
				# if channel is not in bouquets
				bouquetcount = 0
				bouquets = []
				if not bouquetlist is None:
					while True:
						bouquet = bouquetlist.getNext()
						# can we make it easier?
						# or found a way to make another way for that
						if bouquets == []:
							bouquets.append(bouquet)
						else:
							for x in bouquets:
								if x != bouquet:
									bouquets.append(bouquet)
								else:
									bouquetcount += 1
						if bouquetcount >= 5:
							notFound = True
							break

						if bouquet.flags & eServiceReference.isDirectory:
							ChannelSelectionInstance.clearPath()
							ChannelSelectionInstance.setRoot(bouquet)
							servicelist = serviceHandler.list(bouquet)
							if not servicelist is None:
								serviceIterator = servicelist.getNext()
								while serviceIterator.valid():
									if self.service_ref.ref == serviceIterator:
										found = True
										break
									serviceIterator = servicelist.getNext()
								if self.service_ref.ref == serviceIterator:
									found = True
									break
					if found:
						ChannelSelectionInstance.enterPath(rootbouquet)
						ChannelSelectionInstance.enterPath(bouquet)
						ChannelSelectionInstance.saveRoot()
						ChannelSelectionInstance.saveChannel(self.service_ref.ref)
				if found:
					ChannelSelectionInstance.addToHistory(self.service_ref.ref)
			if notFound:
				# Can we get a result for that ?
				# see if you want to delete the running Timer
				self.switchToAll()
			else:
				NavigationInstance.instance.playService(self.service_ref.ref)
			self.justTriedFreeingTuner = True
		else:
			self.log(14, "user didn't want to zap away, record will probably fail")
		self.messageBoxAnswerPending = False

	def switchToAll(self):
		refStr = self.service_ref.ref.toString()
		global InfoBar
		if not InfoBar:
			from Screens.InfoBar import InfoBar
		if refStr.startswith('1:0:2:'):
			if InfoBar.instance.servicelist.mode != 1:
				InfoBar.instance.servicelist.setModeRadio()
				InfoBar.instance.servicelist.radioTV = 1
			InfoBar.instance.servicelist.clearPath()
			rootbouquet = eServiceReference('1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "bouquets.radio" ORDER BY bouquet')
			bouquet = eServiceReference('%s ORDER BY name' % service_types_radio)
		else:
			if InfoBar.instance.servicelist.mode != 0:
				InfoBar.instance.servicelist.setModeTV()
				InfoBar.instance.servicelist.radioTV = 0
			InfoBar.instance.servicelist.clearPath()
			rootbouquet = eServiceReference('1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "bouquets.tv" ORDER BY bouquet')
			bouquet = eServiceReference('%s ORDER BY name' % service_types_tv)
		if InfoBar.instance.servicelist.bouquet_root != rootbouquet:
			InfoBar.instance.servicelist.bouquet_root = rootbouquet
		InfoBar.instance.servicelist.enterPath(bouquet)
		InfoBar.instance.servicelist.setCurrentSelection(self.service_ref.ref)
		InfoBar.instance.servicelist.zap(enable_pipzap=True)
		InfoBar.instance.servicelist.correctChannelNumber()
		InfoBar.instance.servicelist.startRoot = bouquet
		InfoBar.instance.servicelist.addToHistory(self.service_ref.ref)

	def timeChanged(self):
		old_prepare = self.start_prepare
		self.start_prepare = int(self.begin) - config.recording.prepare_time.value #self.prepare_time
		self.backoff = 0

		if old_prepare > 60 and old_prepare != self.start_prepare:
			self.log(15, _("record time changed, start prepare is now: %s") % ctime(self.start_prepare))

	def check_justplay(self):
		if self.justplay:
			self.always_zap = False

	def gotRecordEvent(self, record, event):
		# TODO: this is not working (never true), please fix. (comparing two swig wrapped ePtrs)
		if self.__record_service.__deref__() != record.__deref__():
			return
		# self.log(16, "Record event %d" % event)
		if event == iRecordableService.evRecordWriteError:
			if self.record_service:
				NavigationInstance.instance.stopRecordService(self.record_service)
				self.record_service = None
			self.failed = True
			self.lastend = self.end
			self.end = time() + 5
			self.backoff = 0
			msg, err = self.freespace(True)
			self.log(16, "WRITE ERROR while recording, %s" % msg)
			print("WRITE ERROR on recording, %s" % msg)
			# show notification. the 'id' will make sure that it will be
			# displayed only once, even if more timers are failing at the
			# same time. (which is very likely in case of disk fullness)
			Notifications.AddPopup(text=_("Write error while recording. %s") % (_("An unknown error occurred!"), _("Disk was not found!"), _("Disk is not writable!"), _("Disk full?"))[err], type=MessageBox.TYPE_ERROR, timeout=0, id="DiskFullMessage")
			# ok, the recording has been stopped. we need to properly note
			# that in our state, with also keeping the possibility to re-try.
			# TODO: this has to be done.
		elif event == iRecordableService.evStart:
			text = _("A recording has been started:\n%s") % self.name
			notify = config.usage.show_message_when_recording_starts.value and not Screens.Standby.inStandby
			if self.dirnameHadToFallback:
				text = "\n".join((text, _("Please note that the previously selected media could not be accessed and therefore the default directory is being used instead.")))
				notify = True
			if notify:
				Notifications.AddPopup(text=text, type=MessageBox.TYPE_INFO, timeout=3)
		elif event == iRecordableService.evRecordAborted:
			NavigationInstance.instance.RecordTimer.removeEntry(self)
		elif event == iRecordableService.evGstRecordEnded:
			if self.repeated:
				self.processRepeated(findRunningEvent=False)
			NavigationInstance.instance.RecordTimer.doActivate(self)

	# We have record_service as property to automatically subscribe to record service events.
	def setRecordService(self, service):
		if self.__record_service is not None:
#			print "[remove callback]"
			NavigationInstance.instance.record_event.remove(self.gotRecordEvent)

		self.__record_service = service

		if self.__record_service is not None:
#			print "[add callback]"
			NavigationInstance.instance.record_event.append(self.gotRecordEvent)

	record_service = property(lambda self: self.__record_service, setRecordService)


def createTimer(xml):
	begin = int(xml.get("begin"))
	end = int(xml.get("end"))
	serviceref = ServiceReference(six.ensure_str(xml.get("serviceref")))
	description = six.ensure_str(xml.get("description"))
	repeated = six.ensure_str(xml.get("repeated"))
	rename_repeat = int(xml.get("rename_repeat") or "1")
	disabled = int(xml.get("disabled") or "0")
	justplay = int(xml.get("justplay") or "0")
	always_zap = int(xml.get("always_zap") or "0")
	afterevent = str(xml.get("afterevent") or "nothing")
	afterevent = {
		"nothing": AFTEREVENT.NONE,
		"standby": AFTEREVENT.STANDBY,
		"deepstandby": AFTEREVENT.DEEPSTANDBY,
		"auto": AFTEREVENT.AUTO
		}[afterevent]
	eit = xml.get("eit")
	if eit and eit != "None":
		eit = int(eit)
	else:
		eit = None
	location = xml.get("location")
	if location and location != "None":
		location = six.ensure_str(location)
	else:
		location = None
	tags = xml.get("tags")
	if tags and tags != "None":
		tags = six.ensure_str(tags).split(' ')
	else:
		tags = None
	descramble = int(xml.get("descramble") or "1")
	record_ecm = int(xml.get("record_ecm") or "0")
	isAutoTimer = int(xml.get("isAutoTimer") or "0")
	ice_timer_id = xml.get("ice_timer_id")
	if ice_timer_id:
		ice_timer_id = six.ensure_str(ice_timer_id)

	name = six.ensure_str(xml.get("name"))
	#filename = xml.get("filename").encode("utf-8")
	entry = RecordTimerEntry(serviceref, begin, end, name, description, eit, disabled, justplay, afterevent,
	dirname=location, tags=tags, descramble=descramble, record_ecm=record_ecm,
	isAutoTimer=isAutoTimer, ice_timer_id=ice_timer_id, always_zap=always_zap, rename_repeat=rename_repeat)
	entry.repeated = int(repeated)

	for l in xml.findall("log"):
		time = int(l.get("time"))
		code = int(l.get("code"))
		msg = six.ensure_str(l.text.strip())
		entry.log_entries.append((time, code, msg))

	return entry


class RecordTimer(timer.Timer):
	def __init__(self):
		timer.Timer.__init__(self)

		self.onTimerAdded = []
		self.onTimerRemoved = []
		self.onTimerChanged = []
		self.Filename = resolveFilename(SCOPE_CONFIG, "timers.xml")

		try:
			self.loadTimer()
		except OSError:
			print("unable to load timers from file!")

	def timeChanged(self, entry):
		timer.Timer.timeChanged(self, entry)
		for f in self.onTimerChanged:
			f(entry)

	def cleanup(self):
		for entry in self.processed_timers[:]:
			if not entry.disabled:
				self.processed_timers.remove(entry)
				for f in self.onTimerRemoved:
					f(entry)
		self.saveTimer()

	def doActivate(self, w):
		# when activating a timer which has already passed,
		# simply abort the timer. don't run trough all the stages.
		if w.shouldSkip():
			w.state = RecordTimerEntry.StateEnded
		else:
			# when active returns true, this means "accepted".
			# otherwise, the current state is kept.
			# the timer entry itself will fix up the delay then.
			if w.activate():
				w.state += 1

		try:
			self.timer_list.remove(w)
		except:
			print('[RecordTimer]: Remove list failed')

		# did this timer reach the last state?
		if w.state < RecordTimerEntry.StateEnded:
			# no, sort it into active list
			insort(self.timer_list, w)
		else:
			# yes. Process repeated, and re-add.
			if w.repeated:
				w.processRepeated()
				w.state = RecordTimerEntry.StateWaiting
				w.first_try_prepare = 0 # changed from a bool to a counter, not renamed for compatibility with openWebif
				w.messageBoxAnswerPending = False
				w.justTriedFreeingTuner = False
				w.messageString = "" # incremental MessageBox string
				w.messageStringShow = False
				self.addTimerEntry(w)
			else:
				# check for disabled timers, if time has passed set to completed.
				self.cleanupDisabled()
				# remove old timers as set in config
				self.cleanupDaily(config.recording.keep_timers.value)
				insort(self.processed_timers, w)
		self.stateChanged(w)

	def isRecTimerWakeup(self):
		global wasRecTimerWakeup
		if exists("/tmp/was_rectimer_wakeup"):
			wasRecTimerWakeup = int(open("/tmp/was_rectimer_wakeup", "r").read()) and True or False
		else:
			wasRecTimerWakeup = False
		return wasRecTimerWakeup

	def isRecording(self):
		isRunning = False
		for timer in self.timer_list:
			if timer.isRunning() and not timer.justplay:
				isRunning = True
				break
		return isRunning

	def loadTimer(self):
		# TODO: PATH!
		if not fileExists(self.Filename):
			return
		try:
			file = open(self.Filename, 'r')
			doc = xml.etree.cElementTree.parse(file)
			file.close()
		except SyntaxError:
			from Tools.Notifications import AddPopup
			from Screens.MessageBox import MessageBox

			Notifications.AddPopup(_("The timer file (timers.xml) is corrupt and could not be loaded."), type=MessageBox.TYPE_ERROR, timeout=0, id="TimerLoadFailed")

			print("timers.xml failed to load!")
			try:
				rename(self.Filename, self.Filename + "_old")
			except OSError:
				print("renaming broken timer failed")
			return
		except OSError:
			print("timers.xml not found!")
			return

		root = doc.getroot()

		# display a message when at least one timer overlaps another one
		checkit = True
		for timer in root.findall("timer"):
			newTimer = createTimer(timer)
			if (self.record(newTimer, True, dosave=False) is not None) and (checkit == True):
				from Tools.Notifications import AddPopup
				from Screens.MessageBox import MessageBox
				Notifications.AddPopup(_("Timer overlap in timers.xml detected!\nPlease recheck it!"), type=MessageBox.TYPE_ERROR, timeout=0, id="TimerLoadFailed")
				checkit = False # at the moment it is enough when the message is displayed once

	def saveTimer(self):
		list = ['<?xml version="1.0" ?>\n', '<timers>\n']

		for timer in self.timer_list + self.processed_timers:
			if timer.dontSave:
				continue
			list.append('<timer')
			list.append(' begin="' + str(int(timer.begin)) + '"')
			list.append(' end="' + str(int(timer.end)) + '"')
			list.append(' serviceref="' + stringToXML(str(timer.service_ref)) + '"')
			list.append(' repeated="' + str(int(timer.repeated)) + '"')
			list.append(' rename_repeat="' + str(int(timer.rename_repeat)) + '"')
			list.append(' name="' + str(stringToXML(timer.name)) + '"')
			list.append(' description="' + str(stringToXML(timer.description)) + '"')
			list.append(' afterevent="' + str(stringToXML({
				AFTEREVENT.NONE: "nothing",
				AFTEREVENT.STANDBY: "standby",
				AFTEREVENT.DEEPSTANDBY: "deepstandby",
				AFTEREVENT.AUTO: "auto"
				}[timer.afterEvent])) + '"')
			if timer.eit is not None:
				list.append(' eit="' + str(timer.eit) + '"')
			if timer.dirname is not None:
				list.append(' location="' + str(stringToXML(timer.dirname)) + '"')
			if timer.tags is not None:
				list.append(' tags="' + str(stringToXML(' '.join(timer.tags))) + '"')
			list.append(' disabled="' + str(int(timer.disabled)) + '"')
			list.append(' justplay="' + str(int(timer.justplay)) + '"')
			list.append(' always_zap="' + str(int(timer.always_zap)) + '"')
			list.append(' descramble="' + str(int(timer.descramble)) + '"')
			list.append(' record_ecm="' + str(int(timer.record_ecm)) + '"')
			list.append(' isAutoTimer="' + str(int(timer.isAutoTimer)) + '"')
			if timer.ice_timer_id is not None:
				list.append(' ice_timer_id="' + str(timer.ice_timer_id) + '"')
			list.append('>\n')

			for time, code, msg in timer.log_entries:
				list.append('<log')
				list.append(' code="' + str(code) + '"')
				list.append(' time="' + str(time) + '"')
				list.append('>')
				if isinstance(msg, int):
					list.append(str(msg))
				else:
					list.append(str(stringToXML(msg)))
				list.append('</log>\n')

			list.append('</timer>\n')

		list.append('</timers>\n')

		file = open(self.Filename + ".writing", "w")
		for x in list:
			file.write(x)
		file.flush()

		fsync(file.fileno())
		file.close()
		rename(self.Filename + ".writing", self.Filename)

	def getNextZapTime(self):
		now = time()
		for timer in self.timer_list:
			if not timer.justplay or timer.begin < now:
				continue
			return timer.begin
		return -1

	def getStillRecording(self):
		isStillRecording = False
		now = time()
		for timer in self.timer_list:
			if timer.isStillRecording:
				isStillRecording = True
				break
			elif abs(timer.begin - now) <= 10 and not abs(timer.end - now) <= 10:
				isStillRecording = True
				break
		return isStillRecording

	def getNextRecordingTimeOld(self, getNextStbPowerOn=False):
		now = time()
		if getNextStbPowerOn:
			save_act = -1, 0
			for timer in self.timer_list:
				next_act = timer.getNextActivation(getNextStbPowerOn)
				if timer.justplay or next_act + 3 < now:
					continue
				if debug:
					print("[recordtimer] next stb power up", strftime("%a, %Y/%m/%d %H:%M", localtime(next_act)))
				if save_act[0] == -1:
					save_act = next_act, int(not timer.always_zap)
				else:
					if next_act < save_act[0]:
						save_act = next_act, int(not timer.always_zap)
			return save_act
		else:
			for timer in self.timer_list:
				next_act = timer.getNextActivation()
				if timer.justplay or next_act + 3 < now or timer.end == next_act:
					continue
				return next_act
		return -1

	def getNextRecordingTime(self, getNextStbPowerOn=False):
		#getNextStbPowerOn = True returns tuple -> (timer.begin, set standby)
		nextrectime = self.getNextRecordingTimeOld(getNextStbPowerOn)
		faketime = time() + 300

		if getNextStbPowerOn:
			if config.timeshift.isRecording.value:
				if 0 < nextrectime[0] < faketime:
					return nextrectime
				else:
					return faketime, 0
			else:
				return nextrectime
		else:
			if config.timeshift.isRecording.value:
				if 0 < nextrectime < faketime:
					return nextrectime
				else:
					return faketime
			else:
				return nextrectime

	def isNextRecordAfterEventActionAuto(self):
		for timer in self.timer_list:
			# all types needed True for ident in Navigation.py
			return True
			if timer.justplay:
				continue
			if timer.afterEvent == AFTEREVENT.AUTO or timer.afterEvent == AFTEREVENT.DEEPSTANDBY:
				return True
		return False

	def record(self, entry, ignoreTSC=False, dosave=True): # is called by loadTimer with argument dosave=False
		entry.check_justplay()
		timersanitycheck = TimerSanityCheck(self.timer_list, entry)
		if not timersanitycheck.check():
			if not ignoreTSC:
				print("[RecordTimer] Timer conflict detected!")
				return timersanitycheck.getSimulTimerList()
			else:
				print("ignore timer conflict")
		elif timersanitycheck.doubleCheck():
			print("[RecordTimer] Ignore duplicated timer.")
			return None
		entry.timeChanged()
		print("[RecordTimer] Record %s." % str(entry))
		entry.Timer = self
		self.addTimerEntry(entry)
		# Trigger onTimerAdded callbacks
		for f in self.onTimerAdded:
			f(entry)

		if dosave:
			self.saveTimer()
		return None

	def isInTimer(self, eventid, begin, duration, service, getTimer=False):
		returnValue = None
		type = 0
		timeMatch = 0

		isAutoTimer = 0
		bt = None
		check_offset_time = not config.recording.margin_before.value and not config.recording.margin_after.value
		end = begin + duration
		refstr = ":".join(service.split(":")[:11])
		for timer in self.timer_list:
			isAutoTimer = 0
			if timer.isAutoTimer == 1:
				isAutoTimer |= 1
			if timer.ice_timer_id is not None:
				isAutoTimer |= 2
			check = ":".join(timer.service_ref.ref.toString().split(":")[:11]) == refstr
			if check:
				timer_end = timer.end
				timer_begin = timer.begin
				type_offset = 0
				if not timer.repeated and check_offset_time:
					if 0 < end - timer_end <= 59:
						timer_end = end
					elif 0 < timer_begin - begin <= 59:
						timer_begin = begin
				if timer.justplay:
					type_offset = 5
					if (timer_end - timer.begin) <= 1:
						timer_end += 60
				if timer.always_zap:
					type_offset = 10

				if timer.repeated != 0:
					if bt is None:
						bt = localtime(begin)
						bday = bt.tm_wday
						begin2 = 1440 + bt.tm_hour * 60 + bt.tm_min
						end2 = begin2 + duration // 60
					xbt = localtime(timer.begin)
					xet = localtime(timer_end)
					offset_day = False
					checking_time = timer.begin < begin or begin <= timer.begin <= end
					if xbt.tm_yday != xet.tm_yday:
						oday = bday - 1
						if oday == -1:
							oday = 6
						offset_day = timer.repeated & (1 << oday)
					xbegin = 1440 + xbt.tm_hour * 60 + xbt.tm_min
					xend = xbegin + ((timer_end - timer.begin) // 60)
					if xend < xbegin:
						xend += 1440
					if timer.repeated & (1 << bday) and checking_time:
						if begin2 < xbegin <= end2:
							if xend < end2:
								# recording within event
								timeMatch = (xend - xbegin) * 60
								type = type_offset + 3
							else:
								# recording last part of event
								timeMatch = (end2 - xbegin) * 60
								type = type_offset + 1
						elif xbegin <= begin2 <= xend:
							if xend < end2:
								# recording first part of event
								timeMatch = (xend - begin2) * 60
								type = type_offset + 4
							else:
								# recording whole event
								timeMatch = (end2 - begin2) * 60
								type = type_offset + 2
						elif offset_day:
							xbegin -= 1440
							xend -= 1440
							if begin2 < xbegin <= end2:
								if xend < end2:
									# recording within event
									timeMatch = (xend - xbegin) * 60
									type = type_offset + 3
								else:
									# recording last part of event
									timeMatch = (end2 - xbegin) * 60
									type = type_offset + 1
							elif xbegin <= begin2 <= xend:
								if xend < end2:
									# recording first part of event
									timeMatch = (xend - begin2) * 60
									type = type_offset + 4
								else:
									# recording whole event
									timeMatch = (end2 - begin2) * 60
									type = type_offset + 2
					elif offset_day and checking_time:
						xbegin -= 1440
						xend -= 1440
						if begin2 < xbegin <= end2:
							if xend < end2:
								# recording within event
								timeMatch = (xend - xbegin) * 60
								type = type_offset + 3
							else:
								# recording last part of event
								timeMatch = (end2 - xbegin) * 60
								type = type_offset + 1
						elif xbegin <= begin2 <= xend:
							if xend < end2:
								# recording first part of event
								timeMatch = (xend - begin2) * 60
								type = type_offset + 4
							else:
								# recording whole event
								timeMatch = (end2 - begin2) * 60
								type = type_offset + 2
				else:
					if begin < timer_begin <= end:
						if timer_end < end:
							# recording within event
							timeMatch = timer_end - timer_begin
							type = type_offset + 3
						else:
							# recording last part of event
							timeMatch = end - timer_begin
							type = type_offset + 1
					elif timer_begin <= begin <= timer_end:
						if timer_end < end:
							# recording first part of event
							timeMatch = timer_end - begin
							type = type_offset + 4
							if timer.justplay:
								type = type_offset + 2
						else: # recording whole event
							timeMatch = end - begin
							type = type_offset + 2

				if timeMatch:
					if getTimer:
						returnValue = (timeMatch, type, isAutoTimer, timer)
					else:
						returnValue = (timeMatch, type, isAutoTimer)
					if type in (2, 7, 12): # when full recording do not look further
						break
		return returnValue

	def removeEntry(self, entry):
		print("[RecordTimer] Remove entry '%s'." % str(entry))
		entry.repeated = False  # Avoid re-enqueuing.
		entry.autoincrease = False
		entry.abort()  # Abort timer.  This sets the end time to current time, so timer will be stopped.
		if entry.state != entry.StateEnded:
			self.timeChanged(entry)

#		print "state: ", entry.state
#		print "in processed: ", entry in self.processed_timers
#		print "in running: ", entry in self.timer_list
		# autoincrease instanttimer if possible
		if not entry.dontSave:
			for timer in self.timer_list:
				if timer.setAutoincreaseEnd():
					self.timeChanged(timer)
		if entry in self.processed_timers:  # Now the timer should be in the processed_timers list, remove it from there.
			self.processed_timers.remove(entry)

		# Trigger onTimerRemoved callbacks
		for f in self.onTimerRemoved:
			f(entry)

		self.saveTimer()

	def shutdown(self):
		self.saveTimer()
