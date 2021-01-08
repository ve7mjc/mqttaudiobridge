#!venv/bin/python3

# MQTT Topics
# audio/set/volume 
# audio/play{/volume_int} "sound_file" without extension
# audio/speak{/volume_int} "text to speak"
# audio/play/json - json object with 'sound','volume'
# audio/speak/json - json payload; Required: text; Optional: voice, volume
# audio/announcement/json - json payload - Required: sound, text; Optional: voice, volume

# Python Audio Modules libraries are a real mess (so are the dozen different
#  audio engines). Opted to use simpleaudio and PCM (wav) with ALSA control of 
#  mixer.
# Uses miniaudio library (and ffmpeg if needed) to convert nearly any sound
#  format into wav on the fly and cache it on disk for subsequent requests

# https://docs.aws.amazon.com/polly/latest/dg/API_SynthesizeSpeech.html
# Speech Synthesis Markup Language (SSML) 
#  https://docs.aws.amazon.com/polly/latest/dg/ssml.html
# https://docs.aws.amazon.com/polly/latest/dg/using-conversational.html

import sys
from os import path
import logging
from time import sleep
import subprocess

from pprint import pprint

# static configuration
MQTT_TOPIC_PREFIX = "audio/"
SOUNDS_PATH = "/opt/sounds"
CACHE_PATH = "/opt/sounds/cache"
TTS_WAVEFORM_DB_PATH = "database.json"
SUPPORTED_FORMATS = ['wav']
PREFERRED_FORMAT = 'wav'
TTS_DEFAULT_VOICE = "Matthew"

import paho.mqtt.client as mqtt # pip3 paho-mqtt
import configparser
import argparse
import json

from pathlib import Path

# Audio Modules
import simpleaudio
import alsaaudio as alsa # pip3 pyalsaaudio

# Polly TTS Engine
import boto3
from botocore.config import Config

# audio conversions
import miniaudio # pip3
import array

# config
config = configparser.ConfigParser()
config.read('config.ini')

# Store TTS wave files on disk so we do not hit the server
#  for subsequent requests for same text
class TtsWaveformDatabase():

    def __init__(self,db_path):

        self.db_path = db_path

        self.tts_cache_path = "%s/tts" % CACHE_PATH
        self.filename_base_pattern = self.tts_cache_path + "/tts%05d"
        
        # create cache folder if it does not exist
        Path(self.tts_cache_path).mkdir(parents=True,exist_ok=True)
        
        try:
            with open(db_path) as f:
                self.data = json.load(f)
        except FileNotFoundError as e:
            self.data = {}
            self.data["tts"] = []
    
    def next_filename(self):
        fileno = len(self.data["tts"]) + 1
        return self.filename_base_pattern % fileno
    
    def get_tts(self,text,voice):
        voice = voice.lower()
        for tts in self.data["tts"]:
            if tts["voice"] == voice:
                if tts["text"] == text:
                    return self.tts_cache_path + "/" + tts["filename"]
        # else
        return None
    
    def add_tts(self,text,filename,extensions,voice):
        
        tts = {}
        tts["text"] = text
        tts["filename"] = filename.replace(self.tts_cache_path,"")
        tts["voice"] = voice.lower()
        tts["extensions"] = extensions
        self.data["tts"].append(tts)
        
        self.write_disk()
        
    def write_disk(self):
        with open(self.db_path, 'w') as json_file:
            json.dump(self.data, json_file, indent=4)
    
    def to_json(self):
        return json.dumps(self.data)

logging.basicConfig(stream=sys.stdout, level=logging.DEBUG)
logger = logging.getLogger()


# requires packages: miniaudio, array
def convert_audio_miniaudio(in_file, out_file):

    channels = 1
    sample_rate = 44100

    src = miniaudio.decode_file(in_file) # , dither=miniaudio.DitherMode.TRIANGLE
    
    # DecodedSoundFile - Contains various properties and also the PCM frames of 
    #  a fully decoded audio file.
    tgt = miniaudio.DecodedSoundFile("result", 1, sample_rate, 
        miniaudio.SampleFormat.SIGNED16, array.array('b'))
    
    converted_frames = miniaudio.convert_frames(
        src.sample_format, src.nchannels, src.sample_rate, src.samples.tobytes(),
        tgt.sample_format, tgt.nchannels, tgt.sample_rate)
    
    tgt.num_frames = int(len(converted_frames) / tgt.nchannels / tgt.sample_width)
    tgt.samples.frombytes(converted_frames)

    miniaudio.wav_write_file(out_file, tgt)
    
    logger.debug("wrote converted file to '%s'" % out_file)
    
    return False

# use ffmpeg to convert files -- significantly slower process
#  only to be used if miniaudio is unable
def convert_audio_ffmpeg(filename_input, filename_output):
    
    filename_input = str(filename_input) # "'%s'" % filename_input
    filename_output = str(filename_output) # "'%s'" % filename_output
    
    # ffmpeg -i input.mp3 output.ogg
    channels = 1
    sample_rate = 44100

    # -v fatal - Only show fatal errors. These are errors after which the 
    #            process absolutely cannot continue. 
    ffmpeg = subprocess.run(["/usr/bin/ffmpeg", "-v", "fatal", "-hide_banner", "-nostdin",
                           "-i", filename_input, "-acodec", "pcm_s16le",
                           "-ac", str(channels), "-ar", str(sample_rate), filename_output],
                           capture_output=True, timeout=15)

    logger.debug("called: %s" % ' '.join(ffmpeg.args))

    if ffmpeg.returncode:
        logger.debug("ffmpeg return code: % s" % ffmpeg.returncode)
        logger.debug("ffmpeg stdout: % s" % ffmpeg.stdout)
        logger.debug("ffmpeg stderr: % s" % ffmpeg.stderr)
    
    return ffmpeg.returncode

def convert_audio_file(filename_src, filename_dst):
    logger.debug("converting '%s' to '%s'." % (filename_src,filename_dst))
    convert_audio_miniaudio(filename_src, filename_dst)


class Polly():

    OUTPUT_FORMAT='ogg_vorbis'

    def __init__(self):
        
        self.database = TtsWaveformDatabase(TTS_WAVEFORM_DB_PATH)
        
        self.aws_config = Config(
            region_name = 'us-west-2',
            signature_version = 'v4',
            retries = {
                'max_attempts': 10,
                'mode': 'standard'
            }
        )

    def get_waveform(self,text,voice=None):
        
        if not voice: 
            voice = TTS_DEFAULT_VOICE
        
        engine = "neural"
        text_type = "text"
        text_request = text

        if voice == "Matthew":
            print("detected request for matthew")
            text_type = "ssml"
            text_request = "<speak><amazon:domain name=\"conversational\">%s</amazon:domain></speak>" % text
            # <amazon:effect name="drc"> </amazon:effect>

        # Check for cache first!
        filename_base = self.database.get_tts(text,voice)
        if not filename_base:
            
            # now we initialize the AWS Polly client
            polly = boto3.client('polly', config=self.aws_config)
            
            pollyResponse = polly.synthesize_speech(
                Engine=engine, Text=text_request, OutputFormat=self.OUTPUT_FORMAT, 
                TextType=text_type,VoiceId=voice)

            filename_base = self.database.next_filename()

            filename_ogg = filename_base + ".ogg"
            with open(filename_ogg, 'wb') as f:
                f.write(pollyResponse['AudioStream'].read())

            filename_wav = filename_base + ".wav"
            convert_audio_file(filename_ogg,filename_wav)
            
            self.database.add_tts(text,filename_base,['wav','ogg'],voice)
            
        return filename_base
        

class AudioBridge():

    def __init__(self):

        # create sounds cache folder if it does not exist
        self.sounds_cache_path = "%s/sounds" % CACHE_PATH
        Path(self.sounds_cache_path).mkdir(parents=True,exist_ok=True)

        self.mqttc = mqtt.Client("hmi-audio")
        
        self.mqttc.on_connect = self.on_connect
        self.mqttc.on_message = self.on_message

        self.mqttc.username_pw_set(
            config['mqtt']['username'], config['mqtt']['password'])
        self.mqttc.connect(config['mqtt']['host'])
        
        # set a default volume
        self.master_volume = 30
        
        # volume set requests are generally set as retain
        # we do not want to announce a volume change on start of this script
        self.volume_is_set = False
        
        # Initialize USB Sound Card - Remove Mute and set volume to 100%
        # troubleshoot: aplay -l
        # Onboard sound card is cardindex=0
        # HACK TO FIND THE CARD! C-Media USB Sound card has a mixer 
        #  called "Auto Gain Control"
        cardindex = None
        cards = alsa.cards()
        for i in range(len(cards)):
            mixers = alsa.mixers(cardindex=i)
            for control in mixers:
                if control == "Auto Gain Control":
                    cardindex = i
        if cardindex is None:
            print("unable to find soundcard!")
            exit()

        # Set Mute OFF and device volume to 100%
        self.device_mixer = alsa.Mixer(control="Speaker",cardindex=cardindex)
        self.device_mixer.setmute(0)
        self.device_mixer.setvolume(100)

        # unmute master mixer and set volume to default
        self.master_mixer = alsa.Mixer(control="Master")
        self.master_mixer.setmute(0)
        self.master_mixer.setvolume(self.master_volume)

    def start(self):
        
        logger.info("### started MQTT Audio Bridge")
        
        try:
            self.mqttc.loop_forever()
        except:
            logger.info("shutting down")
            
    def get_tts_waveform(self,text,volume=None,voice=None):
        
        logger.info("speech requested for text='%s', vol=%s, voice=%s" 
            % (text,volume,voice))
        tts = Polly()
        speech_waveform = tts.get_waveform(text,voice)
        
        return speech_waveform

    def speak(self,text,volume=None,voice=None):
        
        waveform = self.get_tts_waveform(text,volume,voice)
        self.play_sound(waveform + ".wav", volume)

    # case and suffix insensitive
    # prefer wav but accept other formats
    def play_sound(self,req_sound,volume):

        filepath = None
        
        # check if we have been supplied a full path to a sound file
        if req_sound.startswith("/") or req_sound.startswith(SOUNDS_PATH):
            filepath = req_sound
            
        else:
            # search local data path for a filename matching base
            for localfile in Path(SOUNDS_PATH).rglob('*'):
                # match base names
                if req_sound.lower() == localfile.stem.lower():
                    filepath = localfile
                    if filepath.suffix[1:] in SUPPORTED_FORMATS:
                        logger.debug("located a sound file in supported format: %s" % localfile)
                        break
            
            if not filepath:
                logger.info("could not locate a suitable sound file for '%s'" % req_sound)
                return True
            
            if filepath and filepath.suffix[1:] not in SUPPORTED_FORMATS:
                logger.debug("located a file as conversion candidate: %s" % filepath)
            
                # convert file to preferred format and write to cache path
                target_file = "%s/%s.%s" % (self.sounds_cache_path,req_sound,PREFERRED_FORMAT)
                if convert_audio_file(filepath,target_file):
                    logger.error("error converting sound file")
                    return True
                    
                filepath = target_file
                
            # check if we have already converted this file

        if filepath:

            logger.info("playing audio file %s at volume %s" % (filepath,volume))
            if volume:
                self.set_volume(volume, True)

            #  SimpleAudio WaveObject 
            try:
                wave_obj = simpleaudio.WaveObject.from_wave_file(str(filepath))
                play_obj = wave_obj.play()            
                play_obj.wait_done() # wait until sound has finished playing
            except Exception as e:
                logger.error("error playing file: %s" % e.__repr__())
            
        else:
            logger.error("could not locate a sound file for '%s'!" % req_sound)

        if volume:
            self.reset_volume()


    # The callback for when the client receives a CONNACK response from the server.
    def on_connect(self,client, userdata, flags, rc):
        if rc==0:
            print("Connected to MQTT broker %s@%s:%s" % 
                (config['mqtt']['username'], config['mqtt']['host'], config['mqtt']['port']))
            subscription = "%s#" % MQTT_TOPIC_PREFIX
            self.mqttc.subscribe(subscription)
            print("Subscribed to %s" % subscription)
        else:
            print("ERROR connecting to MQTT with result code " + str(rc))

    def reset_volume(self):
        volume = self.master_mixer.getvolume()[0]
        if volume != self.master_volume:
            self.master_mixer.setvolume(self.master_volume)

    def set_volume(self,value,temp=False):
        
        if isinstance(value,str):
            value = int(float(value))
            
        if isinstance(value,float):
            value = int(value)
            
        if value >= 0 and value <= 100:
            self.master_mixer.setvolume(value)
            if not temp:
                self.master_volume = value
        else:
            print("ERROR, cannot set volume to '%s'. Must be between 0 and 1." % value)

    # The callback for when a PUBLISH message is received from the server.
    def on_message(self,client,userdata,msg):
        try:
            
            payload = msg.payload.decode('utf-8')

            volume = None

            payload_json = None
            if "/json" in msg.topic:
                
                # JSON Topics
                
                try:
                    payload_json = json.loads(payload)
                except JSONDecodeError as e:
                    print("error decoding: %e" % e.__repr__())
                    return
                    
                # common elements
                volume = payload_json.get("volume", None)
                if volume is None:
                    volume = self.master_volume
                
                # announcement/json
                if (msg.topic.startswith(MQTT_TOPIC_PREFIX + "announcement")) and payload:
                    
                    try:
                        sound = payload_json.get("sound")
                        text = payload_json.get("text")
                        voice = payload_json.get("voice", None)
                    except:
                        logger.error("unable to find required parameters in json: %s" % payload)
                    
                    # obtain tts waveform prior to playing the announcement 
                    #  alert sound to eliminate possible delays
                    tts_waveform = self.get_tts_waveform(text,volume,voice)
                    self.play_sound(sound,float(volume)*0.8)
                    self.play_sound("%s.wav" % tts_waveform,volume)

                # speech/json
                if (msg.topic.startswith(MQTT_TOPIC_PREFIX + "speech")) and payload:
                    text = payload_json.get("text","no text specified")
                    voice = payload_json.get("voice",None)
                    self.speak(text,volume,voice)

                # play/json
                if (msg.topic.startswith(MQTT_TOPIC_PREFIX + "play")) and payload:
                    name = payload_json.get("name")
                    self.play_sound(name,volume)

            else:
            
                # non-JSON messages (dictated by topic)
            
                # SET VOLUME - topic_prefix/set/volume -> 55
                if (msg.topic.startswith(MQTT_TOPIC_PREFIX + "set/volume")) and payload:
                    
                    volume = int(payload)
                    self.set_volume(volume)
                    
                    if self.volume_is_set:
                        self.speak("volume %d" % volume)
                    else:
                        self.volume_is_set = True
                
                if (msg.topic.startswith(MQTT_TOPIC_PREFIX + "speech")) and payload:
                    topic_prefix = MQTT_TOPIC_PREFIX + "speech"
                    pos = msg.topic.rfind("/")
                    if pos == len(topic_prefix):
                        volume = int(msg.topic.split("/")[-1])
                    self.speak(payload,volume)

                if (msg.topic.startswith(MQTT_TOPIC_PREFIX + "play")) and payload:
                    topic_prefix = MQTT_TOPIC_PREFIX + "play"
                    pos = msg.topic.rfind("/")
                    if pos == len(topic_prefix):
                        volume = int(msg.topic.split("/")[-1])
                    self.play_sound(payload,volume)

        except:
            logger.error("on_message() error:", sys.exc_info()[0])


bridge = AudioBridge()
bridge.start()