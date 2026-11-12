# README images

| File | What it shows |
|---|---|
| `chat.webp`, `home.webp`, `avatars.webp`, `voice.webp`, `history.webp`, `settings.webp` | Screenshots of the real app |
| `lipsync-demo.gif` | The still that was uploaded beside frames the lip-sync pipeline generated from it |

Everything here is real output: the screenshots are of the app running against
a real backend, and the lip-sync frames are unedited output of this
repository's MuseTalk pipeline. Please keep it that way — a mock-up in the
README is a promise the software has to keep.

## Whose face

Only publish a face you have the right to publish. The screenshots use the
*Mona Lisa* (public domain). The lip-sync demo uses a sample face that ships
with the [MuseTalk](https://github.com/TMElyralab/MuseTalk) project (MIT).
MuseTalk is trained on photographs, so a painting makes a poor lip-sync demo
even though it works fine as an avatar.

## Retaking the screenshots

With the app running (see the main README's Quick Start):

```bash
mkdir -p /tmp/shots && cd /tmp/shots && npm init -y >/dev/null && npm i playwright-core
cd -                       # back to the repository root
NODE_PATH=/tmp/shots/node_modules PHOTO=/path/to/portrait.jpg node docs/images/capture.mjs

# PNG → WebP at README size
for f in docs/images/raw/*.png; do
  ffmpeg -y -v error -i "$f" -vf scale=1600:-1 -c:v libwebp -quality 88 \
    "docs/images/$(basename "${f%.png}").webp"
done
rm -r docs/images/raw
```

The script signs in as a demo account (created on first run), uploads the
portrait, holds a two-turn conversation and captures each screen once the
notification toasts have cleared. `BASE`, `API`, `CHROME` and `AVATAR_NAME`
are overridable.

## Remaking the lip-sync demo

Generate a clip with the real engine (`AVATAR_ENGINE=musetalk`; a GPU does it
in seconds, a CPU takes a few minutes), then place it beside the source still:

```bash
ffmpeg -y -i still.png -i clip.mp4 -filter_complex \
  "[0:v]scale=320:-2[a];[1:v]scale=320:-2,fps=25[b];[a][b]hstack,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  -loop 0 docs/images/lipsync-demo.gif
```
