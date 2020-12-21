# mqttaudiobridge

Play sound files from disk and high-quality Text-to-Speech via Amazon AWS Polly through MQTT requests.

Developed for use in a Home Automation application and is compatible with [Home Assistant](https://www.home-assistant.io) and other components. Tested only in Linux with the PulseAudio and ALSA audio subsystem (typical Raspberry Pi with Raspberry Pi OS)

**Key Functionality**

 - Manages system volume via ALSA Mixer
 - Stores both Ogg-Vorbis and Wav format AWS responses to disk with a lookup dictionary to re-use on subsequent text-to-speech requests
 - Can play individual requests at a specified volume

**MQTT Topics**

 - {mqtt_topic_prefix}/speech{/optional_volume}
	- payload: text to speak
 - {mqtt_topic_prefix}/play{/optional_volume}
	- payload: filename
- {mqtt_topic_prefix}/set/volume
	- payload: volume as string int

**AWS Polly**

Text-to-Speech uses Amazon AWS Polly and requires AWS credentials to be stored locally:
~/.aws/credentials


