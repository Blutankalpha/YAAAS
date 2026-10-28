# YAAAS

YAAAS (Yet another AI Audio Syncer) is a python script to automatically find, download and/or sync lyrics, by downloading them from LRCLIB, and then syncing them to word by word using WhisperX.

## Setup

**Windows (untested):**

Run `Setup.bat`

**Linux (native):**

```bash
cd /path/to/YAAAS
chmod +x ./setup_linux.sh
chmod +x ./run_linux.sh
./setup_linux.sh (--gpu|--cpu)
```

**Linux (Distrobox):**

```bash
cd /path/to/YAAAS
chmod +x ./setup.sh
chmod +x ./run.sh
./setup.sh (--gpu|--cpu)
```

## Basic Usage

### GUI

**Windows:**

Run `run.bat`

**Linux (native):**

Run `run_linux.sh`

**Linux (Distrobox):**

Run `run.sh`

### No GUI

**Windows:**

```bash
cd /path/to/YAAAS
./run.bat --nogui --out /path/to/output /path/to/input
```

**Linux (Native):**

```bash
cd /path/to/YAAAS
./run_linux.sh --nogui --out /path/to/output /path/to/input
```

**Linux (Distrobox):**

```bash
cd /path/to/YAAAS
./run.sh --nogui --out /path/to/output /path/to/input
```

## CLI options

### Setup

- `--help` - lists available command line options.
- `-h` - alias of `--help`
- `--CPU` - Configures the program using torch on CPU
- `--GPU` - Configures the program using CUDA acceleration
- `--recreate` - Distrobox only, removes and readds the distrobox.

### Run

- `--help` - lists available command line options.
- `-h` - alias of `--help`
- `--nogui` - runs the program without a GUI
- `--gui` - default flag, runs with a GUI, deprecated.
- `--scan /path/to/music` - goes through an existing folder structure recursively, and automatically syncs or makes any lyrics.
- `--upgrade` - redoes any existing lyrics that were not created by YAAAS
- `--out /path/to/output` - sets the output directory of the program
- `-o /path/to/output` - alias of `--out`
- `--order (auto|artist-title|title-artist)` - defines whether the input file is listed with artist then the title or title then artist.
- `--threshold #` - sets the minimum acceptable fuzzy match score.
- `--no-expand` - only downloads the songs selected and not the whole album that the song is in.
- `--format (mp3|m4a|opus|flac)` - changes the file type that music is downloaded as. keep in mind that audio is downloaded from YT, so it always begins as a 128 mp3.
- `--lyrics (all|requested|none)` - what lyrics are created, whether its every song, only the songs you requested (not the others in the album) or no lyrics at all.
- `--demucs-model (htdemucs|htdemucs_ft)` - which demucs version to use
- `--language (iso 3166 code)` - what language the songs are in, sometimes has more accurate transcription.
- `--no-transcribe` - only syncs lyrlib or local lyrics, and doesnt create them.
- `--transcribe-model (small|medium|large-v3)` - which whisper size to use
- `--yes` - skips manual confirmation.
- `-y` - alias of `--yes`
- `--force` - redoes all lyrics.
- `--check` - verifies the install.

## Input file structure

### txt

One item per line.

Each item should be either title - artist or artist - title.

Example:

```text
“Of The Devil” Selected Works - Zachery Cabral
Yeat - 2093
smart - Le Sserafim
```

### json

Supported as it is the output from a private project of mine.

Not recommended to use.

Example:

```json
[
  {
    "title": "Keep it together",
    "recording_mbid": "ec11208d-8ab9-4f6d-adbc-a4b65dcd31b8",
    "artist": "Zachery Cabral",
    "album": '"Of The Devil" Selected Works',
    "artist_mbid": "376312b0-086d-4455-8ee3-4cea1f624b93",
    "musicbrainz_url": "musicbrainz.org/recording/ec11208d-8ab9-4f6d-adbc-a4b65dcd31b8",
    "kind": "track",
  },
]
```

## Why did I make this?

Because there's so many vibecoded projects that do the same thing, but are full of errors, misuse storage space and hardware, and are so fragmented, I decided to make a definitive final "One to rule them all" software.

![XKCD: 927](https://imgs.xkcd.com/comics/standards.png)

## AI use disclosure.

This project was an opportunity to learn how to use AI for programming. While the code is predominantly written by me, I used claude.ai and their "Sonnet 5.5" model at "Medium" effort for debugging, and for documentation. It's output has been checked by me.
