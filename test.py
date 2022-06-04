#!venv/bin/python3

# MQTT Topics
# audio/set/volume 
# audio/play{/volume_int} "sound_file" without extension
# audio/speak{/volume_int} "text to speak"
# audio/play/json - json object with 'name','volume'
# audio/speak/json - json payload; Required: text; Optional: voice, volume
# audio/announcement/json - json payload - Required: sound, text; Optional: voice, volume

# dont forget!
# Paho MQTT Client loop operates in loop, loop_start, and loop_forever
# loop() tends to the receive/transmit buffers
# loop_start() starts a thread
# loop_forever() blocks forever
#

from time import sleep

# static configuration
MQTT_TOPIC_PREFIX = "audio"
SOUNDS_PATH = "/opt/sounds"
CACHE_PATH = "/opt/sounds/cache"
TTS_WAVEFORM_DB_PATH = "database.json"
SUPPORTED_FORMATS = ['wav']
PREFERRED_FORMAT = 'wav'
TTS_DEFAULT_VOICE = "Matthew"

import paho.mqtt.client as mqtt # pip3 paho-mqtt

# Config
import configparser
config = configparser.ConfigParser()
config.read('config.ini')

connected = False

def doTest():    
    for vol in range(0,100,5):
        msg = vol
        topic = f"{MQTT_TOPIC_PREFIX}/speak/{vol}"
        print(f"volume: {topic} -> {msg}")
        mqttc.publish(topic, msg)
        mqttc.loop()
        sleep(2)

def on_connect(client, userdata, flags, rc):
    connected = True
    print("connected.")
    doTest()

def on_message(self,client,userdata,msg):
    pass

def on_disconnect():
    print("disconnected!")

mqttc = mqtt.Client("hmi-audio-test")

mqttc.on_connect = on_connect
mqttc.on_message = on_message
mqttc.on_disconnect = on_disconnect

# mqttc.username_pw_set(
#     config['mqtt']['username'], 
#     config['mqtt']['password'])

print("connecting..")
mqttc.connect(config['mqtt']['host'])

# we will break this
mqttc.loop_start()

while not connected:
    sleep(1)

print(f"connected")

doTest()
