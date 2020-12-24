#!venv/bin/python3

import sys
from os import path
import logging

# static configuration
mqtt_topic_prefix = "audio/"
sound_files_folder = "/opt/sounds/"
data_storage_path = "data/"
database_path = "database.json"

import paho.mqtt.client as mqtt
import configparser
import argparse
import json

# Audio Modules
import simpleaudio as sa
import alsaaudio as alsa # ALSA Mixer

# POLLY TTS
import boto3
from botocore.config import Config

# Ogg to Wav
import miniaudio
import array

# config
config = configparser.ConfigParser()
config.read('config.ini')

# Store TTS wave files on disk so we do not hit the server
#  for subsequent requests for same text
class WaveformDatabase():

    def __init__(self,disk_path):
        
        self.disk_path = disk_path
        self.filename_pattern = data_storage_path + "tts%05d"
        self.data_path = data_storage_path
        
        try:
            with open(disk_path) as f:
                self.data = json.load(f)
        except FileNotFoundError as e:
            self.data = {}
            self.data["tts"] = []
    
    def next_filename(self):
        fileno = len(self.data["tts"]) + 1
        return self.filename_pattern % fileno
    
    def get_tts(self,text,voice):
        voice = voice.lower()
        for tts in self.data["tts"]:
            if tts["voice"] == voice:
                if tts["text"] == text:
                    return self.data_path + tts["filename"]
        return None
    
    def add_tts(self,text,filename,extensions,voice):
        
        tts = {}
        tts["text"] = text
        tts["filename"] = filename.replace(self.data_path,"")
        tts["voice"] = voice.lower()
        tts["extensions"] = extensions
        self.data["tts"].append(tts)
        
        self.write_disk()
        
    def write_disk(self):
        with open(self.disk_path, 'w') as json_file:
            json.dump(self.data, json_file, indent=4)
    
    def to_json(self):
        return json.dumps(self.data)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger()


# requires packages: miniaudio, array
def convert_ogg_to_wav(in_file, out_file):

    src = miniaudio.decode_file(in_file, dither=miniaudio.DitherMode.TRIANGLE)
    result = miniaudio.DecodedSoundFile("result", 1, 22050, miniaudio.SampleFormat.UNSIGNED8, array.array('b'))
    
    converted_frames = miniaudio.convert_frames(src.sample_format, src.nchannels, src.sample_rate, src.samples.tobytes(),
                                        result.sample_format, result.nchannels, result.sample_rate)
    
    result.num_frames = int(len(converted_frames) / result.nchannels / result.sample_width)
    result.samples.frombytes(converted_frames)

    miniaudio.wav_write_file(out_file, result)


class Polly():

    OUTPUT_FORMAT='ogg_vorbis'

    def __init__(self, voice):
        
        self.database = WaveformDatabase(database_path)
        
        my_config = Config(
            region_name = 'us-west-2',
            signature_version = 'v4',
            retries = {
                'max_attempts': 10,
                'mode': 'standard'
            }
        )
        
        self.polly = boto3.client('polly', config=my_config) # amazon web service
        self.voice = voice

    def get_waveform(self, text, voice=None):
        
        if not voice: voice = self.voice
        
        # Check for cache first!
        filename_base = self.database.get_tts(text,voice)
        if not filename_base:
            pollyResponse = self.polly.synthesize_speech(Text=text, OutputFormat=self.OUTPUT_FORMAT, VoiceId=voice)

            filename_base = self.database.next_filename()

            filename_ogg = filename_base + ".ogg"
            with open(filename_ogg, 'wb') as f:
                f.write(pollyResponse['AudioStream'].read())

            filename_wav = filename_base + ".wav"
            convert_ogg_to_wav(filename_ogg,filename_wav)
            
            self.database.add_tts(text,filename_base,['wav','ogg'],voice)
            
        return filename_base


class AudioBridge():

    def __init__(self):

        self.mqttc = mqtt.Client("hmi-audio")
        
        self.mqttc.on_connect = self.on_connect
        self.mqttc.on_message = self.on_message

        self.mqttc.username_pw_set(config['mqtt']['username'], config['mqtt']['password'])
        self.mqttc.connect(config['mqtt']['host'])
        
        # default volume
        self.master_volume = 30
        
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
        logger.info("Started HMI-Audio")
        self.mqttc.loop_forever()

    def speak(self,text,volume=None,voice=None):
        
        if not voice:
            voice = "Joanna"
        logger.info("speech requested for text='%s', vol=%s, voice=%s" 
            % (text,volume,voice))
        tts = Polly(voice)
        self.play(tts.get_waveform(text) + ".wav",volume)

    def play(self,filename,volume=None):
        
        filepath = filename
        
        # append our base storage folder if a folder is not specified
        if "/" not in filepath:
            filepath = sound_files_folder + filename
        
        if path.exists(filepath):
            logger.info("playing audio file %s" % filename)
            if volume:
                self.set_volume(volume,True)
            wave_obj = sa.WaveObject.from_wave_file(filepath)
            play_obj = wave_obj.play()
            play_obj.wait_done()  # Wait until sound has finished playing
        else:
            logger.error("file '%s' does not exist!" % filename)

        if volume:
            self.reset_volume()

    # The callback for when the client receives a CONNACK response from the server.
    def on_connect(self,client, userdata, flags, rc):
        if rc==0:
            print("Connected to MQTT broker %s@%s:%s" % (config['mqtt']['username'], config['mqtt']['host'], config['mqtt']['port']))
            subscription = "%s#" % mqtt_topic_prefix
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
        
            print("%s %s" % (msg.topic,payload))
        
            volume = None

            payload_json = None
            if "/json" in msg.topic:
                
                # JSON Topics
                
                try:
                    payload_json = json.loads(payload)
                except JSONDecodeError as e:
                    print("error decoding: %e" % e.__repr__())
                    return
                
                if (msg.topic.startswith(mqtt_topic_prefix + "speech")) and payload:
                    print(payload_json)
                    text = payload_json.get("text","no text specified")
                    volume = payload_json.get("volume",None)
                    voice = payload_json.get("voice",None)
                    self.speak(text,volume,voice)

                if (msg.topic.startswith(mqtt_topic_prefix + "play")) and payload:
                    topic_prefix = mqtt_topic_prefix + "play"
                    pos = msg.topic.rfind("/")
                    if pos == len(topic_prefix):
                        volume = int(msg.topic.split("/")[-1])
                    self.play(payload,volume)

            else:
            
                # non-JSON topics
            
                if (msg.topic.startswith(mqtt_topic_prefix + "set/volume")) and payload:
                    volume = int(payload)
                    self.set_volume(volume)
                    self.speak("volume %d" % volume)
                
                if (msg.topic.startswith(mqtt_topic_prefix + "speech")) and payload:
                    topic_prefix = mqtt_topic_prefix + "speech"
                    pos = msg.topic.rfind("/")
                    if pos == len(topic_prefix):
                        volume = int(msg.topic.split("/")[-1])
                    self.speak(payload,volume)

                if (msg.topic.startswith(mqtt_topic_prefix + "play")) and payload:
                    topic_prefix = mqtt_topic_prefix + "play"
                    pos = msg.topic.rfind("/")
                    if pos == len(topic_prefix):
                        volume = int(msg.topic.split("/")[-1])
                    self.play(payload,volume)

        except:
            logger.error("on_message() error:", sys.exc_info()[0])


bridge = AudioBridge()
bridge.start()