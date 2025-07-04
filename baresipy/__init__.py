from time import sleep
import pexpect
from opentone import ToneGenerator
from responsive_voice import ResponsiveVoice
from pydub import AudioSegment
import tempfile
import logging
import subprocess
from threading import Thread
from baresipy.utils import create_daemon
from baresipy.utils.log import LOG
import baresipy.config
from os.path import expanduser, join, isfile, isdir
from os import makedirs
import signal
import re

logging.getLogger("urllib3.connectionpool").setLevel("WARN")
logging.getLogger("pydub.converter").setLevel("WARN")


class BareSIP(Thread):
    def __init__(self, user, pwd, gateway, transport="udp", other_sip_configs="", tts=None, debug=False,
                 block=True, config_path=None, sounds_path=None, auto_start_process=True):
        config_path = config_path or join("~", ".baresipy")
        self.config_path = expanduser(config_path)
        if not isdir(self.config_path):
            makedirs(self.config_path)
        if isfile(join(self.config_path, "config")):
            with open(join(self.config_path, "config"), "r") as f:
                self.config = f.read()
            LOG.info("config loaded from " + self.config_path + "/config")
            self.updated_config = False
        else:
            self.config = baresipy.config.DEFAULT
            self.updated_config = True

        self._original_config = str(self.config)

        if sounds_path is not None and "#audio_path" in self.config:
            self.updated_config = True
            if sounds_path is False:
                # sounds disabled
                self.config = self.config.replace(
                    "#audio_path		/usr/share/baresip",
                    "audio_path		/dont/load")
            elif isdir(sounds_path):
                self.config = self.config.replace(
                    "#audio_path		/usr/share/baresip",
                    "audio_path		" + sounds_path)

        if self.updated_config:
            with open(join(self.config_path, "config.bak"), "w") as f:
                f.write(self._original_config)

            LOG.info("saving config")
            with open(join(self.config_path, "config"), "w") as f:
                f.write(self.config)

        self.debug = debug
        self.user = user
        self.pwd = pwd
        self.gateway = gateway
        self.transport = transport
        self.block = block
        self.started_once = False
        self._prev_output = ""
        self.running = False
        self.ready = False
        self.mic_muted = False
        self.abort = False
        self.current_call = None
        self._call_status = None
        self.audio = None
        self._ts = None
        self.baresip = None

        if tts:
            self.tts = tts
        else:
            self.tts = ResponsiveVoice(gender=ResponsiveVoice.MALE)
        self._server = "sip:{u}@{g}".format(u=self.user, g=self.gateway)
        self._login = self._server + ";transport={t};auth_pass={p};{o};".format(
            p=self.pwd, t=self.transport, o=other_sip_configs
        ).replace(';;', ';').rstrip(';')

        if auto_start_process:
            self.start_process()

    def start_process(self):
        if self.started_once:
            LOG.error('Python Thread Instance cannot be started more than once. Please create a new BareSIP instance')
            return
        self.started_once = True
        super().__init__()
        self.start()
        if self.block:
            self.wait_until_ready()

    # properties
    @property
    def call_established(self):
        return self.call_status == "ESTABLISHED"

    @property
    def call_status(self):
        return self._call_status or "DISCONNECTED"

    # actions
    def do_command(self, action):
        if self.ready:
            action = str(action)
            self.baresip.sendline(action)
        else:
            LOG.warning(action + " not executed!")
            LOG.error("NOT READY! please wait")

    def login(self):
        LOG.info("Adding account: " + self.user)
        self.baresip.sendline("/uanew " + self._login)
    
    def logout(self):
        LOG.info("Removing account: " + self.user)
        self.baresip.sendline("/uadelall")

    def call(self, number):
        LOG.info("Dialling: " + number)
        self.do_command("/dial " + number)

    def hang(self):
        if self.current_call:
            LOG.info("Hanging: " + self.current_call)
            self.do_command("/hangup")
            self.current_call = None
            self._call_status = None
        else:
            LOG.error("No active call to hang")

    def hold(self):
        if self.current_call:
            LOG.info("Holding: " + self.current_call)
            self.do_command("/hold")
        else:
            LOG.error("No active call to hold")

    def resume(self):
        if self.current_call:
            LOG.info("Resuming " + self.current_call)
            self.do_command("/resume")
        else:
            LOG.error("No active call to resume")

    def mute_mic(self):
        if not self.call_established:
            LOG.error("Can not mute microphone while not in a call")
            return
        if not self.mic_muted:
            LOG.info("Muting mic")
            self.do_command("/mute")
        else:
            LOG.info("Mic already muted")

    def unmute_mic(self):
        if not self.call_established:
            LOG.error("Can not unmute microphone while not in a call")
            return
        if self.mic_muted:
            LOG.info("Unmuting mic")
            self.do_command("/mute")
        else:
            LOG.info("Mic already unmuted")

    def accept_call(self):
        self.do_command("/accept")
        status = "ESTABLISHED"
        self.handle_call_status(status)
        self._call_status = status

    def list_calls(self):
        self.do_command("/listcalls")

    def check_call_status(self):
        self.do_command("/callstat")
        sleep(0.1)
        return self.call_status
    
    def killBareSIPSubProcess(self):
        if self.baresip != None and self.baresip.isalive():
            LOG.info("Killing BareSip process")
            self.baresip.sendline("/quit")
            self.baresip.close()
            self.baresip.kill(signal.SIGKILL)
        self.baresip = None # this would prompt the run() loop to call startBareSIPProcess
    
    def startBareSIPSubProcess(self):
        LOG.info("Starting BareSip process")
        self.baresip = pexpect.spawn('baresip -f ' + self.config_path)

    def quit(self):
        if self.updated_config:
            LOG.info("restoring original config")
            with open(join(self.config_path, "config"), "w") as f:
                f.write(self._original_config)
        LOG.info("Closing BareSIP instance")
        if self.running:
            if self.current_call:
                self.hang()

            self.killBareSIPSubProcess()
            self.current_call = None
            self._call_status = None
            self.abort = True
            self.running = False

    def send_dtmf(self, number):
        number = str(number)
        for n in number:
            if n not in "0123456789":
                LOG.error("invalid dtmf tone")
                return
        LOG.info("Sending dtmf tones for " + number)
        dtmf = join(tempfile.gettempdir(), number + ".wav")
        ToneGenerator().dtmf_to_wave(number, dtmf)
        self.send_audio(dtmf)

    def speak(self, speech):
        if not self.call_established:
            LOG.error("Speaking without an active call!")
        else:
            LOG.info("Sending TTS for " + speech)
            self.send_audio(self.tts.get_mp3(speech))
            sleep(0.5)

    def send_audio(self, wav_file):
        if not self.call_established:
            LOG.error("Can't send audio without an active call!")
            return
        wav_file, duration = self.convert_audio(wav_file)
        # send audio stream
        LOG.info("transmitting audio")
        self.do_command("/ausrc aufile," + wav_file)
        # wait till playback ends
        sleep(duration - 0.5)
        # avoid baresip exiting
        self.do_command("/ausrc alsa,default")

    @staticmethod
    def convert_audio(input_file, outfile=None):
        input_file = expanduser(input_file)
        sound = AudioSegment.from_file(input_file)
        sound += AudioSegment.silent(duration=500)
        # ensure minimum time
        # workaround baresip bug
        while sound.duration_seconds < 3:
            sound += AudioSegment.silent(duration=500)

        outfile = outfile or join(tempfile.gettempdir(), "pybaresip.wav")
        sound = sound.set_frame_rate(48000)
        sound = sound.set_channels(2)
        sound.export(outfile, format="wav")
        return outfile, sound.duration_seconds

    # this is played out loud over speakers
    def say(self, speech):
        if not self.call_established:
            LOG.warning("Speaking without an active call!")
        self.tts.say(speech, blocking=True)

    def play(self, audio_file, blocking=True):
        if not audio_file.endswith(".wav"):
            audio_file, duration = self.convert_audio(audio_file)
        self.audio = self._play_wav(audio_file, blocking=blocking)

    def stop_playing(self):
        if self.audio is not None:
            self.audio.kill()

    @staticmethod
    def _play_wav(wav_file, play_cmd="aplay %1", blocking=False):
        play_mp3_cmd = str(play_cmd).split(" ")
        for index, cmd in enumerate(play_mp3_cmd):
            if cmd == "%1":
                play_mp3_cmd[index] = wav_file
        if blocking:
            return subprocess.call(play_mp3_cmd)
        else:
            return subprocess.Popen(play_mp3_cmd)

    # events
    def handle_incoming_call(self, number):
        LOG.info("Incoming call: " + number)
        if self.call_established:
            LOG.info("already in a call, rejecting")
            sleep(0.1)
            self.do_command("b")
        else:
            LOG.info("default behaviour, rejecting call")
            sleep(0.1)
            self.do_command("b")

    def handle_call_rejected(self, number):
        LOG.info("Rejected incoming call: " + number)

    def handle_call_timestamp(self, timestr):
        LOG.info("Call time: " + timestr)

    def handle_call_status(self, status):
        if status != self._call_status:
            LOG.debug("Call Status: " + status)

    def handle_call_start(self):
        number = self.current_call
        LOG.info("Calling: " + number)

    def handle_call_ringing(self):
        number = self.current_call
        LOG.info(number + " is Ringing")

    def handle_call_established(self):
        LOG.info("Call established")

    def handle_call_ended(self, reason, number=None):
        LOG.info("Call ended")
        LOG.debug(f"Number: {number} , Reason: {reason}")

    def _handle_no_accounts(self):
        LOG.debug("No accounts setup")
        self.login()

    def handle_login_success(self):
        LOG.info("Logged in!")

    def handle_login_failure(self):
        LOG.error("Log in failed!")
        self.quit()

    def handle_ready(self):
        LOG.info("Ready for instructions")

    def handle_mic_muted(self):
        LOG.info("Microphone muted")

    def handle_mic_unmuted(self):
        LOG.info("Microphone unmuted")

    def handle_audio_stream_failure(self):
        LOG.debug("Aborting call, maybe we reached voicemail?")
        self.hang()

    def handle_dtmf_received(self, char, duration):
        LOG.info("Received DTMF symbol '{0}' duration={1}".format(char, duration))

    def handle_error(self, error):
        LOG.error(error)
        if error == "failed to set audio-source (No such device)":
            self.handle_audio_stream_failure()
            
    def handle_unhandled_output(self, output):
        LOG.info("Received unhandled output: '{0}'".format(output))

    # event loop
    def run(self):
        self.running = True
        while self.running:
            try:
                if self.baresip == None:
                    self.startBareSIPSubProcess()
                    continue
                if not self.baresip.isalive():
                    self.killBareSIPSubProcess()
                    sleep(0.5)
                    continue

                out = self.baresip.readline().decode("utf-8")

                if out != self._prev_output:
                    out = out.strip()
                    if self.debug:
                        LOG.debug(out)
                    if "baresip is ready." in out:
                        self.handle_ready()
                    elif "account: No SIP accounts found" in out:
                        self._handle_no_accounts()
                    # elif "All 1 useragent registered successfully!" in out or\
                    elif "200 OK" in out:
                        self.ready = True
                        self.handle_login_success()
                    elif "ua: SIP register failed:" in out or\
                            "401 Unauthorized" in out or \
                            "Register: Destination address required" in out or\
                            "Register: Connection timed out" in out:
                        self.handle_error(out)
                        self.handle_login_failure()
                    elif "Incoming call from: " in out:
                        num = out.split("Incoming call from: ")[
                            1].split(" - (press 'a' to accept)")[0].strip()
                        self.current_call = num
                        self._call_status = "INCOMING"
                        self.handle_incoming_call(num)
                    elif "call: rejecting incoming call from " in out:
                        num = out.split("rejecting incoming call from ")[1].split(" ")[0].strip()
                        self.handle_call_rejected(num)
                    elif "call: SIP Progress: 180 Ringing" in out:
                        self.handle_call_ringing()
                        status = "RINGING"
                        self.handle_call_status(status)
                        self._call_status = status
                    elif "call: connecting to " in out:
                        n = out.split("call: connecting to '")[1].split("'")[0]
                        self.current_call = n
                        self.handle_call_start()
                        status = "OUTGOING"
                        self.handle_call_status(status)
                        self._call_status = status
                    elif "Call established:" in out:

                        status = "ESTABLISHED"
                        self.handle_call_status(status)
                        self._call_status = status
                        sleep(0.5)
                        self.handle_call_established()
                    elif "call: hold " in out:
                        n = out.split("call: hold ")[1]
                        status = "ON HOLD"
                        self.handle_call_status(status)
                        self._call_status = status
                    elif "Call with " in out and \
                            "terminated (duration: " in out:
                        status = "DISCONNECTED"
                        duration = out.split("terminated (duration: ")[1][:-1]
                        self.handle_call_status(status)
                        self._call_status = status
                        self.handle_call_timestamp(duration)
                        self.mic_muted = False
                    elif "call muted" in out:
                        self.mic_muted = True
                        self.handle_mic_muted()
                    elif "call un-muted" in out:
                        self.mic_muted = False
                        self.handle_mic_unmuted()
                    elif "session closed:" in out:
                        reason = out.split("session closed:")[1].strip()
                        number = out.split(": session closed:")[0].strip()
                        status = "DISCONNECTED"
                        self.handle_call_status(status)
                        self._call_status = status
                        self.handle_call_ended(reason, number)
                        self.mic_muted = False
                    elif "(no active calls)" in out:
                        status = "DISCONNECTED"
                        self.handle_call_status(status)
                        self._call_status = status
                    elif "===== Call debug " in out:
                        status = out.split("(")[1].split(")")[0]
                        self.handle_call_status(status)
                        self._call_status = status
                    elif "--- List of active calls (1): ---" in \
                            self._prev_output:
                        if "ESTABLISHED" in out and self.current_call in out:
                            ts = out.split("ESTABLISHED")[0].split(
                                "[line 1]")[1].strip()
                            if ts != self._ts:
                                self._ts = ts
                                self.handle_call_timestamp(ts)
                    elif "failed to set audio-source (No such device)" in out:
                        error = "failed to set audio-source (No such device)"
                        self.handle_error(error)
                    elif "terminated by signal" in out or "ua: stop all" in \
                            out:
                        self.running = False
                    elif "received DTMF:" in out:
                        match = re.search('received DTMF: \'(.)\' \(duration=(\d+)\)', out)
                        if match:
                            self.handle_dtmf_received(match.group(1), int(match.group(2)))
                    else:
                        self.handle_unhandled_output(out)
                    self._prev_output = out
            except pexpect.exceptions.EOF:
                # baresip exited
                self.running = False
            except pexpect.exceptions.TIMEOUT:
                # nothing happened for a while
                pass
            except KeyboardInterrupt:
                self.running = False
            except:
                # Uncaught Exception. Gracefully quit
                self.running = False

        self.quit()

    def wait_until_ready(self):
        while not self.ready:
            sleep(0.1)
            if self.abort:
                return

