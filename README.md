A simple but highly efficient personal voice assistant(can be enhanced)

Steps for mainv4 file
- run the mainv4 python file for higher effiency and speech recognition
- run python mainv4.py --list-devices (to capture all speaker and microphone drivers in your device)
- run python mainv4.py --device <INDEX_OF_YOUR_MICROPHONE> --model small --compute int8 --debug --bypass-wake
Note:
-You can use medium instead of small in the --model (for higher accuracy of speech to text )
-Anyway small is enough for most buds

Steps for mainv1,v2,v3 files

The mainv1.py requires VOSK models (which i dont recommend using {ABS TRASH})

-still if you want to give it a try download it from https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip

-place it in the project folder and run the python mainv1.py --list device

-select the index of your microphone(usually it would be 1 or 2)

-run python mainv1.py --device  <INDEX> --debug ( to check your microphone is working and correct index)

-run python mainv1.py --device <INDEX> --debug --bypass-wake

This assistant is quite simple but its more than enough for daily tasks and  automating repeatating works
Its  lightweight and high accuracy for day-to-day tasks.
