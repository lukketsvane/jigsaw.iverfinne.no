"""Collect the upper melody track's simultaneous note-on events for tap playback."""
from io import BytesIO
from itertools import groupby
from pathlib import Path
from struct import unpack


def read_vlq(stream):
    value = 0
    while True:
        byte = stream.read(1)[0]
        value = (value << 7) | (byte & 0x7f)
        if byte < 0x80:
            return value


def track_notes(data):
    stream = BytesIO(data)
    notes = []
    tick = 0
    status = None
    while stream.tell() < len(data):
        tick += read_vlq(stream)
        first = stream.read(1)[0]
        if first == 0xff or first in (0xf0, 0xf7):
            if first == 0xff:
                stream.read(1)  # meta event type
            stream.read(read_vlq(stream))
            status = None
            continue
        if first >= 0x80:
            status = first
            first = stream.read(1)[0]
        kind = status >> 4
        if kind in (0xc, 0xd):  # program change and channel pressure take one byte
            continue
        second = stream.read(1)[0]
        if kind == 0x9 and second:
            notes.append((tick, first, second))
    return notes


def melody_chords(path):
    source = BytesIO(Path(path).read_bytes())
    tag, length = unpack('>4sI', source.read(8))
    if tag != b'MThd':
        raise ValueError(f'{path} has no MIDI header')
    _, count, _ = unpack('>HHH', source.read(length)[:6])
    tracks = []
    for _ in range(count):
        tag, length = unpack('>4sI', source.read(8))
        if tag != b'MTrk':
            raise ValueError(f'{path} has an invalid track header')
        notes = track_notes(source.read(length))
        if notes:
            tracks.append(notes)
    melody = max(tracks, key=lambda notes: sum(n[1] for n in notes) / len(notes))
    return [[(pitch, velocity) for _, pitch, velocity in chord]
            for _, chord in groupby(melody, key=lambda note: note[0])]
