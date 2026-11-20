"""Retire the chat photo worker or set an explicit Strata trial profile on its host."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

RESERVE = "--vram-reserve-later-mib"
ARRAYS = ("args", "args_written", "args_derived", "user_args")


def configure(config, *, retire=False, context=None, slots=None):
    if not isinstance(config.get("args"), list):
        raise ValueError("Нет массива args в конфигурации Strata")
    split_engine = bool(config.get("split_engine_note"))
    for name in ARRAYS:
        if name not in config:
            continue
        args = config[name]
        if not isinstance(args, list):
            raise ValueError(f"Неизвестный формат {name}")
        for flag, value in ((RESERVE, None), ("--max-context", context), ("--batch", slots), ("--slots", slots)):
            if (flag == RESERVE and not retire) or (flag != RESERVE and value is None):
                continue
            if any(isinstance(a, str) and a.startswith((flag + "=", flag + " ")) for a in args):
                raise ValueError(f"Неизвестный формат {flag} в {name}")
            # The serve CLI rejects an explicit engine slot count alongside --parallel.
            for i in reversed(range(len(args))):
                arg = args[i]
                if arg != flag:
                    continue
                if i + 1 == len(args) or not str(args[i + 1]).isdigit():
                    raise ValueError(f"Некорректное значение {flag} в {name}")
                if flag in ("--batch", "--slots") and not split_engine:
                    del args[i:i + 2]
                elif flag == RESERVE:
                    # Only replace the two budgets previously set for diffusion.
                    if str(args[i + 1]) in ("9216", "16384"):
                        args[i + 1] = "2048"
                else:
                    args[i + 1] = str(value)
            if name == "args" and flag not in args and (flag == "--max-context" or (flag == "--batch" and split_engine and "--slots" not in args)):
                args.extend((flag, str(value)))
    if slots is not None:
        config["parallel"] = slots


def retire_worker(home):
    unit = "mira-photo.service"
    systemctl = ["systemctl", "--user"]
    status = subprocess.run(systemctl + ["show", unit, "-p", "LoadState", "--value"],
                            capture_output=True, text=True)
    if status.stdout.strip() != "not-found":
        status.check_returncode()
        subprocess.run(systemctl + ["disable", "--now", unit], check=True)
    folder = home / "mira-photo"
    if folder.is_symlink():
        raise ValueError("Каталог mira-photo является ссылкой")
    (home / ".config/systemd/user" / unit).unlink(missing_ok=True)
    subprocess.run(systemctl + ["daemon-reload"], check=True)
    for name in ("worker.py", "photo-worker.key", unit, "upgrade-xl.sh", "mira-photo-worker.tar.gz"):
        (folder / name).unlink(missing_ok=True)
    venv = folder / ".venv"
    if not venv.is_symlink() and (venv / "pyvenv.cfg").is_file():
        shutil.rmtree(venv)


def active_profile():
    unit = "englishbot-huihui.service"
    pid = subprocess.check_output(["systemctl", "show", unit, "-p", "MainPID", "--value"], text=True).strip()
    if not pid.isdigit() or int(pid) == 0:
        raise ValueError("Служба Strata остановлена. Укажите путь через --profile")
    proc = Path("/proc") / pid
    argv = [os.fsdecode(v) for v in (proc / "cmdline").read_bytes().split(b"\0") if v]
    profiles = []
    for i, argument in enumerate(argv):
        flag, sep, value = argument.partition("=")
        if flag in ("--profile", "--config"):
            if not sep:
                value = argv[i + 1] if i + 1 < len(argv) else ""
            if not value or value.startswith("--"):
                raise ValueError("Пустой путь к профилю службы")
            path = Path(value)
            profiles.append(path if path.is_absolute() else (proc / "cwd").resolve() / path)
    if len(set(profiles)) != 1:
        raise ValueError("Не удалось определить единственный профиль службы. Укажите путь через --profile")
    return profiles[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, help="JSON профиля (по умолчанию из команды запущенной службы)")
    parser.add_argument("--retire-photos", action="store_true")
    parser.add_argument("--context", type=int, choices=(32768, 65536, 131072))
    parser.add_argument("--slots", type=int, choices=(1, 2, 4, 6))
    args = parser.parse_args()
    if not args.retire_photos and args.context is None:
        parser.error("Укажите --retire-photos или --context и --slots")
    if (args.context is None) != (args.slots is None):
        parser.error("--context и --slots передаются вместе")
    home = Path.home()
    path = args.profile if args.profile is not None else active_profile()
    if path.is_symlink():
        raise ValueError("Конфигурация Strata является ссылкой")
    raw = path.read_bytes()
    data = json.loads(raw)
    configure(data, retire=args.retire_photos, context=args.context, slots=args.slots)
    if args.retire_photos:
        retire_worker(home)
    if data != json.loads(raw):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + "." + stamp + ".bak")
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
            temp = Path(out.name)
            try:
                out.write((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
                out.flush()
                os.fsync(out.fileno())
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as saved:
                    saved.write(raw)
                    saved.flush()
                    os.fsync(saved.fileno())
                if path.read_bytes() != raw:
                    raise RuntimeError("Конфигурация изменилась параллельно")
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
        print("Резервная копия:", backup)
    print("Профиль подготовлен:", path)


if __name__ == "__main__":
    main()
