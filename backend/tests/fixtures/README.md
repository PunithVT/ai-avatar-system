# Test fixtures

`speech_two_sentences.flac` — 7.2 s, 16 kHz mono. Two Harvard sentences with a
pause of about 1.1 s between them:

| Speech (s) | Text |
|---|---|
| 0.7 – 3.2 | "The birch canoe slid on the smooth planks." |
| 4.35 – 6.6 | "Glue the sheet to the dark blue background." |

Voice-activity and end-of-turn models only react to real speech, so tests of
turn-taking need a real recording rather than a synthetic tone.

Source: the first seconds of `OSR_us_000_0010_8k.wav` from the
[Open Speech Repository](https://www.voiptroubleshooter.com/open_speech/),
whose recordings of the IEEE Harvard sentences are provided freely for speech
and telephony testing. Resampled from 8 kHz.
