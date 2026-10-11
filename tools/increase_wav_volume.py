import argparse
import math
import shutil
import struct
import tempfile
import wave
from pathlib import Path

#
#


def increase_volume(path: Path, gain_db: float, apply: bool) -> None:
    with wave.open(str(path), "rb") as sound:
        params = sound.getparams()

        if params.sampwidth != 2 or params.comptype != "NONE":
            raise ValueError(f"{path}: expected uncompressed 16-bit PCM WAV")

        frames = sound.readframes(params.nframes)

    if not frames or len(frames) != params.nframes * params.nchannels * 2:
        raise ValueError(f"{path}: empty or truncated WAV")

    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    peak = max(abs(sample) for sample in samples)

    if peak == 0:
        print(f"{path.name}: silent, skipped")
        return

    available_db = max(0.0, 20 * math.log10(32767 / peak))
    actual_db = min(gain_db, available_db)

    if actual_db == 0:
        print(f"{path.name}: no clipping-free gain available, skipped")
        return

    gain = 10 ** (actual_db / 20)
    amplified = struct.pack(f"<{len(samples)}h", *(round(sample * gain) for sample in samples))
    backup = path.with_suffix(".wav.bak")

    if apply:
        with tempfile.TemporaryDirectory(dir=path.parent) as directory:
            output = Path(directory) / path.name

            with wave.open(str(output), "wb") as sound:
                sound.setparams(params)
                sound.writeframes(amplified)

            with path.open("rb") as source, backup.open("xb") as destination:
                shutil.copyfileobj(source, destination)

            output.replace(path)

    action = "applied" if apply else "preview"
    print(f"{path.name}: +{actual_db:.2f} dB ({action}; requested +{gain_db:g} dB)")


#
#


def main() -> None:
    parser = argparse.ArgumentParser(description="Increase WAV sample volume without clipping; preview by default")
    parser.add_argument("--folder", type=Path, default=Path(__file__).resolve().parents[1] / "system" / "sounds" / "cz")
    parser.add_argument("--gain-db", type=float, default=6.0, help="Requested gain in decibels (default: 6)")
    parser.add_argument("--apply", action="store_true", help="Replace WAVs and save originals as .wav.bak; refuses existing backups")
    args = parser.parse_args()

    if not math.isfinite(args.gain_db) or args.gain_db <= 0:
        parser.error("--gain-db must be a finite positive number")

    if not args.folder.is_dir():
        parser.error(f"Folder does not exist: {args.folder}")

    paths = sorted(args.folder.glob("*.wav"))

    if not paths:
        parser.error(f"No WAV files found in {args.folder}")

    if args.apply:
        for path in paths:
            backup = path.with_suffix(".wav.bak")

            if backup.exists():
                parser.error(f"Backup already exists: {backup}")

    try:
        for path in paths:
            increase_volume(path, args.gain_db, args.apply)
    except (OSError, EOFError, wave.Error, ValueError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
